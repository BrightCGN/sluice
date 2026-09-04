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
- `PiiRegexMode` (Rev. 12) — reversible=False. Regex-Stufe allein, span-basiert (§5.3).
- `PiiNerMode` (Rev. 12) — reversible=False. `pii_regex` **plus** Modellerkennung,
  additiv; Unterklasse von `PiiRegexMode`, damit die Regex-Stufe nicht wegfallen kann.
  Braucht den NER-Dienst (§7.5) — ohne ihn blockiert der Guard fail-closed (§5.3).

Ein späteres Verfahren (format-preserving, post-v1) ist einfach ein weiterer Modus
hinter demselben Schalter — ohne Guard/Verifier/Audit anzufassen.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from sluice.content import Surfaces, messages_surfaces
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
    tools:            die vom Konsumenten deklarierten Tool-Specs (neutral, §7.2, Rev. 15).
                      Sie stehen HIER und nicht als Dispatch-Parameter, damit es keinen
                      Weg gibt, Tools zu senden, ohne dass der Guard sie sieht — die
                      Chokepoint-Eigenschaft ist strukturell, nicht per Konvention (§1).
    """

    raw_text: str
    generalized_text: str | None = None
    messages: list[dict[str, Any]] | None = None
    tools: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class Sanitized:
    """Ergebnis von `forward()` — der Egress-Kandidat nach dem Modus."""

    text: str | None = None
    messages: list[dict[str, Any]] | None = None

    def surfaces(self) -> Surfaces:
        """Alle Flächen, die der Verifier prüfen muss (§5/§5.5).

        Nicht nur `content:str`: Block-Listen, Tool-Result-Inhalte und Tool-Argumente
        gehören dazu (`sluice/content.py`). Was sich nicht aufzählen lässt, kommt als
        `opaque` zurück — darauf blockiert der Guard fail-closed, statt es ungeprüft
        durchzulassen.
        """
        base = Surfaces(texts=(self.text,)) if self.text is not None else Surfaces()
        if self.messages is None:
            return base
        return base.merge(messages_surfaces(self.messages))


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
    """Registriert einen Modus unter `name` (öffentlicher Erweiterungspunkt, §3).

    Lädt zuerst die Built-ins. Ohne das hinge das Ergebnis an der Aufrufreihenfolge: wer
    sich registriert, *bevor* irgendetwas die Built-ins angefasst hat, würde von der
    späteren Lazy-Ladung überschrieben — der Erweiterungspunkt wäre je nach Importpfad
    still wirkungslos. Eine Registrierung überschreibt einen gleichnamigen Built-in
    bewusst (das ist der Sinn: einen Modus ersetzbar machen).
    """
    _ensure_builtins()
    _MODE_FACTORIES[name] = factory


def _register_builtin(name: str, factory: ModeFactory) -> None:
    """Interne Registrierung ohne `_ensure_builtins` — sonst Rekursion beim Laden."""
    _MODE_FACTORIES[name] = factory


def _ensure_builtins() -> None:
    global _builtins_loaded
    if _builtins_loaded:
        return
    # VOR den Registrierungen setzen: `_register_builtin` ruft `_ensure_builtins` zwar
    # nicht auf, aber die Modul-Importe unten könnten es indirekt tun.
    _builtins_loaded = True
    from sluice.modes.generalizing import GeneralizingMode
    from sluice.modes.passthrough import PassthroughMode
    from sluice.modes.pii_ner import PiiNerMode
    from sluice.modes.pii_regex import PiiRegexMode
    from sluice.modes.pseudonymizing import PseudonymizingMode
    from sluice.modes.strict import StrictMode

    _register_builtin(
        "strict",
        lambda p: StrictMode(
            detector_profile=p.detector_profile, dictionary_terms=p.dictionary_terms
        ),
    )
    _register_builtin("passthrough", lambda p: PassthroughMode())
    _register_builtin("generalizing", lambda p: GeneralizingMode())
    _register_builtin(
        "pseudonymizing",
        lambda p: PseudonymizingMode(ttl_seconds=p.reversible.ttl_seconds if p.reversible else 3600),
    )
    # Rev. 12 — die zweistufige PII-Erkennung (§5.3). `pii_ner` ist Unterklasse von
    # `pii_regex`: die Regex-Stufe ist strukturell dieselbe, das Modell kommt additiv dazu.
    _register_builtin(
        "pii_regex",
        lambda p: PiiRegexMode(
            detector_profile=p.detector_profile, dictionary_terms=p.dictionary_terms
        ),
    )
    _register_builtin(
        "pii_ner",
        lambda p: PiiNerMode(
            detector_profile=p.detector_profile,
            dictionary_terms=p.dictionary_terms,
            ner_config=p.ner,
        ),
    )


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
