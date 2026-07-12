"""Modus-Schalter — Interface + Auswahl (Spec §2/§3, Rev. 9).

Der Schalter wählt eine *Modus-Implementierung* aus der Registry, kein `if reversible:`
im Guard. Die sicheren Defaults (Profil-Gate, Verifier, Audit) verändert er NIE — er
tauscht nur das Modus-Objekt (Spec §3).

- `StrictMode` — reversible=False, Auslieferungs-Default (§4.3). Auto-redigierend.
- `PassthroughMode` — kein Verifier, keine Transformation; explizites Opt-in (§2.1).
- `GeneralizingMode` — reversible=False. Einbahnstraße; verifiziert konsument-generalisierten Text.
- `PseudonymizingMode` — reversible=True, explizites Opt-in per Profil oder
  Request-`mode: "reversible"` (§7.2): die schwächere Zusage (Mapping-Tabelle bleibt
  personenbezogen) und führt Zustand ein — nie geerbt, immer bewusst deklariert.

Ein späteres Verfahren (format-preserving, post-v1) ist einfach ein weiterer Modus
hinter demselben Schalter — ohne Guard/Verifier/Audit anzufassen.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from sluice.policy import Profile


@dataclass(frozen=True)
class Scope:
    """Ein Mapping-Scope (Session-Bindung, Spec §8). Für generalizing bedeutungslos."""

    key: str


@dataclass(frozen=True)
class EgressPayload:
    """Der Egress-Kandidat, wie ihn der Konsument anliefert.

    raw_text:         das Konkrete — nur fürs reviewbare Vorher/Nachher + Audit (§6).
    generalized_text: generalizing: der vom Konsumenten *semantisch generalisierte*
                      Text (die Generalisierung ist Konsumenten-Domäne, §1.1 — NICHT Sluice).
    messages:         pseudonymizing: role/content-Message-Liste (Proxy-Form, §7.2).
    """

    raw_text: str
    generalized_text: str | None = None
    messages: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class Sanitized:
    """Ergebnis von `forward()` — der Egress-Kandidat nach dem Modus."""

    text: str | None = None
    messages: list[dict[str, Any]] | None = None

    def texts(self) -> list[str]:
        """Alle Textflächen, die der Verifier prüfen muss (§5)."""
        out: list[str] = []
        if self.text is not None:
            out.append(self.text)
        for m in self.messages or ():
            content = m.get("content")
            if isinstance(content, str):
                out.append(content)
        return out


class StreamReverserProtocol(Protocol):
    def feed(self, chunk: str) -> str: ...
    def flush(self) -> str: ...


@runtime_checkable
class Mode(Protocol):
    """Das Modus-Interface (Spec §3) — die Registry-Einheit des Schalters (Rev. 9).

    Ein Modus deklariert seine Eigenschaften selbst:
    - `reversible`: trägt einen Mapping-Rückweg (nur PseudonymizingMode).
    - `enforce_verifier`: ob der Guard den deterministischen Riegel (§5) *unter* diesem
      Modus fail-closed komponiert. `True` für die Sanitisierungs-Modi (`strict`,
      `generalizing`, `pseudonymizing`), `False` für `passthrough` (§2.1). Der Modus
      entscheidet damit, ob der Verifier greift — der Guard erzwingt ihn nicht global.
    """

    reversible: bool
    name: str
    enforce_verifier: bool

    async def forward(self, payload: EgressPayload, scope: Scope | None) -> Sanitized:
        """Roh → Egress-Kandidat (redigiert/generalisiert/pseudonymisiert/unverändert)."""
        ...

    async def reverse_text(self, text: str, scope: Scope) -> str:
        """Sanitisiert → roh. NUR reversible=True. Sonst NotImplementedError."""
        ...

    def stream_reverser(self, scope: Scope) -> StreamReverserProtocol:
        """Holdback-Puffer: ein Pseudonym kann über zwei Chunks reichen. NUR reversible=True."""
        ...

    async def reverse_obj(self, obj: dict[str, Any], scope: Scope) -> dict[str, Any]:
        """Tool-Call-Argumente zurückmappen. NUR reversible=True."""
        ...


# ---- Modus-Registry (Spec §3, Rev. 9) ------------------------------------------------
#
# Der Erweiterungspunkt: Dritte registrieren eigene Modi über `register_mode`. Eine
# Factory bekommt das Profil und baut die Modus-Instanz (z. B. mit `detector_profile`
# oder Mapping-TTL). Eingebaute Modi werden lazy registriert, damit die Modul-Importe
# der Modus-Klassen (die aus diesem Paket importieren) keinen Zyklus bilden.

ModeFactory = Callable[["Profile"], Mode]

_MODE_FACTORIES: dict[str, ModeFactory] = {}
_builtins_loaded = False

# Modi mit Zustand (Mapping-Lebenszyklus §8) müssen pro Profil stabil bleiben; wir
# cachen alle Instanzen pro (Profil, Modus), damit Scopes über Guard-Aufrufe halten.
_instances: dict[str, Mode] = {}


def register_mode(name: str, factory: ModeFactory) -> None:
    """Registriert einen Modus unter `name` (öffentlicher Erweiterungspunkt, §3)."""
    _MODE_FACTORIES[name] = factory


def _ensure_builtins() -> None:
    global _builtins_loaded
    if _builtins_loaded:
        return
    from sluice.modes.generalizing import GeneralizingMode
    from sluice.modes.passthrough import PassthroughMode
    from sluice.modes.pseudonymizing import PseudonymizingMode
    from sluice.modes.strict import StrictMode

    register_mode(
        "strict",
        lambda p: StrictMode(
            detector_profile=p.detector_profile, dictionary_terms=p.dictionary_terms
        ),
    )
    register_mode("passthrough", lambda p: PassthroughMode())
    register_mode("generalizing", lambda p: GeneralizingMode())
    register_mode(
        "pseudonymizing",
        lambda p: PseudonymizingMode(ttl_seconds=p.reversible.ttl_seconds if p.reversible else 3600),
    )
    _builtins_loaded = True


def registered_modes() -> set[str]:
    """Alle bekannten Modus-Namen (Built-ins + Dritt-Registrierungen)."""
    _ensure_builtins()
    return set(_MODE_FACTORIES)


def is_registered_mode(name: str) -> bool:
    """Ob `name` ein registrierter Modus ist (für Profil-Validierung, §4)."""
    _ensure_builtins()
    return name in _MODE_FACTORIES


def select_mode(profile: Profile) -> Mode:
    """Zieht aus `profile.mode` die Modus-Instanz aus der Registry (Spec §3)."""
    _ensure_builtins()
    factory = _MODE_FACTORIES.get(profile.mode)
    if factory is None:  # fail-closed: unbekannter Modus ist ein Konfigurationsfehler
        raise ValueError(f"Unbekannter Modus '{profile.mode}' in Profil '{profile.name}'.")

    # Cache-Key enthält den Modus: ein Request-`mode`-Override (§7.2) desselben Profils
    # darf nie die Instanz eines anderen Modus erwischen.
    cache_key = f"{profile.name}:{profile.mode}"
    cached = _instances.get(cache_key)
    if cached is not None:
        return cached

    instance = factory(profile)
    _instances[cache_key] = instance
    return instance


__all__ = [
    "EgressPayload",
    "Mode",
    "ModeFactory",
    "Sanitized",
    "Scope",
    "StreamReverserProtocol",
    "is_registered_mode",
    "register_mode",
    "registered_modes",
    "select_mode",
]
