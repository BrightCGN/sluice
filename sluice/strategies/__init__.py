"""Strategie-Schalter — Interface + Auswahl (Spec §2/§3).

Der Schalter wählt eine *Strategie-Implementierung*, kein `if reversible:` im Guard.
Die drei Invarianten (Profil-Gate, Verifier, Audit) verändert er NIE — er tauscht
nur das Strategie-Objekt (Spec §2).

- `GeneralizingStrategy` — reversible=False, DEFAULT (Rev. 4, safety first). Einbahnstraße.
- `PseudonymizingStrategy` — reversible=True, explizites Opt-in per Profil oder
  Request-`mode: "reversible"` (§7.2): die schwächere DSGVO-Zusage (Mapping-Tabelle
  bleibt personenbezogen) und führt Zustand ein — nie geerbt, immer bewusst deklariert.

Ein späteres drittes Verfahren (format-preserving, post-v1) ist einfach eine weitere
`SanitizationStrategy` hinter demselben Schalter — ohne Guard/Verifier/Audit anzufassen.
"""

from __future__ import annotations

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
    """Ergebnis von `forward()` — der Egress-Kandidat nach der Strategie."""

    text: str | None = None
    messages: list[dict[str, Any]] | None = None

    def texts(self) -> list[str]:
        """Alle Textflächen, die der Verifier prüfen muss (Invariante 2)."""
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
class SanitizationStrategy(Protocol):
    """Das Strategie-Interface (Spec §3) — der Mechanismus-Kern des Schalters."""

    reversible: bool
    name: str

    async def forward(self, payload: EgressPayload, scope: Scope | None) -> Sanitized:
        """Roh → sanitisiert (generalisiert ODER pseudonymisiert). Egress-Kandidat."""
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


# ---- Auswahl über das Profil (Spec §2: der Schalter) ---------------------------------

# Pseudonymisierende Strategien tragen Zustand (Mapping-Lebenszyklus §8) und werden
# deshalb pro Profil gecacht, damit Scopes über mehrere Guard-Aufrufe stabil bleiben.
_instances: dict[str, SanitizationStrategy] = {}


def select_strategy(profile: Profile) -> SanitizationStrategy:
    """Zieht aus `profile.strategy` die Implementierung (Spec §3)."""
    from sluice.strategies.generalizing import GeneralizingStrategy
    from sluice.strategies.pseudonymizing import PseudonymizingStrategy

    # Cache-Key enthält die Strategie: ein Request-`mode`-Override (§7.2) desselben
    # Profils darf nie die Instanz des anderen Modus erwischen.
    cache_key = f"{profile.name}:{profile.strategy}"
    cached = _instances.get(cache_key)
    if cached is not None:
        return cached

    strategy: SanitizationStrategy
    if profile.strategy == "generalizing":
        strategy = GeneralizingStrategy()
    elif profile.strategy == "pseudonymizing":
        rev = profile.reversible
        strategy = PseudonymizingStrategy(
            ttl_seconds=rev.ttl_seconds if rev else 3600,
        )
    else:  # fail-closed: unbekannte Strategie ist ein Konfigurationsfehler
        raise ValueError(f"Unbekannte Strategie '{profile.strategy}' in Profil '{profile.name}'.")

    _instances[cache_key] = strategy
    return strategy


__all__ = [
    "EgressPayload",
    "Sanitized",
    "SanitizationStrategy",
    "Scope",
    "StreamReverserProtocol",
    "select_strategy",
]
