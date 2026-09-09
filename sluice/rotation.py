"""Modell-Rotation — bewusst wechselnde KIs innerhalb der Profil-Allowlist
(Spec §4.5, Revision 16).

**Warum das in Sluice liegt.** Crate rotiert heute selbst: es wählt je Lauf einen
Eintrag aus `[[sluice.rotation]]` und schickt `provider`/`model` mit
(`orchestrator/curation.py::select_rotation`). Der Grund ist ausdrücklich *nicht*
Kosten oder Ausfallsicherheit, sondern **Geschmacksvielfalt** — die Kuratierung ist
zustandslos, also darf jeder Lauf ein anderes Modell fragen. Dieselbe Logik in jedem
Projekt neu zu bauen ist genau die Fragmentierung, die Sluice beendet (§1); und die
Auswahlmenge ist ohnehin schon eine Sluice-Sache, weil sie die Provider-Allowlist
(§4.1) nicht verlassen darf.

**Was Mechanismus ist und was Domäne (§1.1).** Sluice führt die Auswahl aus; *welche*
Modelle ein Konsument als austauschbar ansieht, ist Domänenwissen und wird deshalb im
Profil **deklariert** — dasselbe Muster wie die Detektor-Muster (§5.1) und die
Provider-Allowlist. Sluice erfindet keine Äquivalenzen.

**Zwei Opt-ins, beide ausdrücklich.**

1. Das **Profil** deklariert `[profile.<name>.rotation]` — ohne diesen Block gibt es
   keine Rotation. Jeder Eintrag muss in der `provider_allowlist` stehen; das wird
   **beim Laden** geprüft (`policy.py`), damit die Auswahlmenge konstruktionsbedingt
   nicht aus der Allowlist ausbrechen kann. Der Guard prüft sie danach trotzdem noch
   einmal (§4.1) — doppelt geprüft ist an einer Boundary kein Makel.
2. Der **Request** fragt sie mit `model: "auto"` an (§7.2). Ein konkret genanntes
   Modell wird **nie** stillschweigend ersetzt: wer `claude-opus-5` verlangt, bekommt
   `claude-opus-5` oder eine Absage, nie unbemerkt etwas anderes. Ein Chokepoint, der
   das Ziel austauscht, ohne dass jemand es merkt, verletzt dieselbe Regel wie einer,
   der Tools verschluckt (§7.2).

**Zustandslos (Rev. 16).** Die Politik `random` hält *keinen* Zustand: kein Zeiger,
der einen Neustart nicht überlebt, keine Instanz-Drift, wenn mehrere Kerne laufen, und
keine Kopplung an einen Scope (§8). Über viele Läufe verteilt sie gleich; in einer
kleinen Stichprobe darf dasselbe Modell zweimal hintereinander drankommen — das ist
der bewusst gezahlte Preis dafür, dass niemand einen Rotationsstand pflegen muss.
`headroom` nutzt zusätzlich die Kapazitäts-Telemetrie (§7.6), die ohnehin nur
best-effort ist; fehlt sie, fällt die Politik auf Zufall **innerhalb derselben
Kandidatenmenge** zurück — das ist keine Lockerung einer Sicherheitszusage, sondern
nur eine gleichförmigere Verteilung (die Allowlist gilt unverändert).

**Kein stilles Ausweichen.** Eine Auswahl, ein Aufruf. Scheitert der gewählte
Provider, ist das ein Fehler mit Namen — kein Retry auf einem anderen. Sonst verstecke
man genau die Information, die man sammeln will: welches Modell unzuverlässig ist.
Failover/Health/Retry bleiben ausdrücklich draußen (§10).
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from sluice.capacity import CapacityStore

AUTO_MODEL = "auto"  # der Request-Wert, der Rotation anfragt (§7.2)

#: Auswahl-Politiken. `random` ist der Default und hält keinen Zustand; `headroom`
#: bevorzugt den Provider mit dem größten gemeldeten Restkontingent (§7.6).
VALID_POLICIES = ("random", "headroom")
DEFAULT_POLICY = "random"


class RotationError(RuntimeError):
    """Rotation angefragt, aber nicht bedienbar — fail-closed, nie ein Ersatzziel."""


@dataclass(frozen=True)
class RotationEntry:
    """Ein Modell in der Rotationsmenge eines Profils (§4.5).

    `min_output_tokens` gehört **je Eintrag** hierher, nicht global: ein
    Reasoning-Modell verbraucht sein Budget zuerst mit internem Nachdenken, ein
    direkt antwortendes nicht. Wer das Modell wählt, verantwortet das Token-Budget —
    sonst schickt der Konsument eine Zahl für Modell A und bekommt Modell B, und die
    Antwort ist **leer** statt abgeschnitten (in Crate beobachtet, CRATE-SLUICE §12.2).
    """

    provider: str
    model: str
    min_output_tokens: int = 0


@dataclass(frozen=True)
class RotationConfig:
    """Die deklarierte Rotationsmenge eines Profils (§4.5). Leer = keine Rotation."""

    policy: str = DEFAULT_POLICY
    entries: tuple[RotationEntry, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.entries)


@dataclass(frozen=True)
class RotationChoice:
    """Die getroffene Wahl **samt Begründung**.

    Die Begründung ist kein Komfort: sobald Sluice das Ziel wählt, ist es nicht mehr
    allein aus dem Profil ableitbar. Sie geht deshalb ins Audit (§6), damit ein
    Eintrag weiterhin die vollständige Auskunft darüber ist, wohin etwas ging und
    warum dorthin.
    """

    entry: RotationEntry
    reason: str

    @property
    def provider(self) -> str:
        return self.entry.provider

    @property
    def model(self) -> str:
        return self.entry.model


def effective_max_tokens(requested: int, entry: RotationEntry) -> int:
    """Token-Budget nach der Wahl: **anheben, nie senken** (§4.5, CRATE-SLUICE §12.2).

    Der Konsument bemisst `max_tokens` an seiner Zielgröße — das reicht für ein
    Modell, das sofort antwortet. Wählt Sluice ein Reasoning-Modell, ist das Budget
    aufgebraucht, bevor das erste Zeichen kommt. Deshalb hebt die Untergrenze des
    gewählten Eintrags den Wert an. Senken darf sie ihn nie: der Konsument weiß, wie
    lang seine Antwort werden muss, Sluice nicht.
    """
    return max(int(requested), int(entry.min_output_tokens))


def select_rotation(
    config: RotationConfig,
    *,
    provider: str | None = None,
    capacity: CapacityStore | None = None,
    rng: random.Random | None = None,
) -> RotationChoice:
    """Wählt einen Eintrag aus der deklarierten Menge (§4.5).

    provider: engt die Menge auf einen Provider ein — nennt der Request *ihn*
              ausdrücklich und das Modell mit `auto`, ist das die Aussage „dieser
              Anbieter, welches seiner Modelle ist mir gleich". Passt kein Eintrag,
              wird **abgewiesen**, nicht auf einen anderen Anbieter ausgewichen.
    capacity: Telemetriestand für `policy = "headroom"`; ohne ihn zählt jeder
              Kandidat als unbekannt und die Wahl fällt auf Zufall zurück.
    rng:      Injektionspunkt für Tests. Im Betrieb das Modul-`random`.

    Wirft `RotationError`, wenn nichts bleibt — fail-closed.
    """
    if not config.entries:
        raise RotationError(
            "Rotation angefragt (model: 'auto'), aber das Profil deklariert keine "
            "Rotationsmenge (§4.5)."
        )

    candidates = config.entries
    if provider:
        # Kanonisch vergleichen (§7.3): Profile schreiben historisch `claude`, der
        # Provider-Lock (Rev. 5) und `canonical_provider` sprechen `anthropic`. Ein
        # literaler Vergleich würde hier eine Rotationsmenge leeren, die in Wahrheit
        # passt — und die Absage nennte dann einen Grund, der nicht der wahre ist.
        from sluice.providers import canonical_provider  # lazy: hält policy.py leicht

        wanted = canonical_provider(provider)
        candidates = tuple(e for e in candidates if canonical_provider(e.provider) == wanted)
        if not candidates:
            raise RotationError(
                f"Rotation angefragt für Provider '{provider}', aber die Rotationsmenge "
                f"des Profils enthält keinen Eintrag dieses Providers (§4.5) — "
                f"fail-closed statt Ausweichen auf einen anderen Anbieter."
            )

    chooser = rng if rng is not None else random
    if len(candidates) == 1:
        only = candidates[0]
        return RotationChoice(
            entry=only,
            reason=f"policy={config.policy}: einziger Kandidat {only.provider}/{only.model}",
        )

    if config.policy == "headroom":
        return _by_headroom(candidates, config.policy, capacity, chooser)

    picked = chooser.choice(list(candidates))
    return RotationChoice(
        entry=picked,
        reason=(
            f"policy=random (zustandslos): {picked.provider}/{picked.model} "
            f"aus {len(candidates)} Kandidaten"
        ),
    )


def _by_headroom(
    candidates: tuple[RotationEntry, ...],
    policy: str,
    capacity: CapacityStore | None,
    chooser: random.Random | object,
) -> RotationChoice:
    """`policy = "headroom"`: größtes gemeldetes Restkontingent gewinnt (§7.6).

    Ein Kandidat **ohne** Telemetrie gilt als unbekannt und nimmt am Vergleich nicht
    teil, statt als „voll" durchzugehen: sonst gewänne dauerhaft der Provider, über
    den man am wenigsten weiß. Weiß man über *keinen* etwas — frische Instanz, ein
    Provider ohne Rate-Limit-Header —, entscheidet der Zufall. Bei Gleichstand
    ebenfalls: eine feste Reihenfolge wäre hier nur eine unsichtbare Bevorzugung.
    """
    measured: list[tuple[float, RotationEntry]] = []
    if capacity is not None:
        for entry in candidates:
            headroom = capacity.headroom(entry.provider)
            if headroom is not None:
                measured.append((headroom, entry))

    if not measured:
        picked = chooser.choice(list(candidates))  # type: ignore[union-attr]
        return RotationChoice(
            entry=picked,
            reason=(
                f"policy={policy}: kein Kapazitätsstand für einen der {len(candidates)} "
                f"Kandidaten (§7.6) → Zufall: {picked.provider}/{picked.model}"
            ),
        )

    best = max(h for h, _ in measured)
    tied = [e for h, e in measured if h == best]
    picked = tied[0] if len(tied) == 1 else chooser.choice(tied)  # type: ignore[union-attr]
    return RotationChoice(
        entry=picked,
        reason=(
            f"policy={policy}: größter gemeldeter Headroom {best:.3f} → "
            f"{picked.provider}/{picked.model} "
            f"({len(measured)}/{len(candidates)} Kandidaten mit Telemetrie)"
        ),
    )


__all__ = [
    "AUTO_MODEL",
    "DEFAULT_POLICY",
    "VALID_POLICIES",
    "RotationChoice",
    "RotationConfig",
    "RotationEntry",
    "RotationError",
    "effective_max_tokens",
    "select_rotation",
]
