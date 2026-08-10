"""Detektor-Profile — Muster pro Domäne, Engine geteilt (Spec §5.1).

Die Verifier-*Engine* (sluice/verifier.py) ist generischer Mechanismus; *welche*
Identifier für eine Domäne zählen, deklariert der Konsument über sein Profil
(`detector_profile`, Spec §4). Diese Trennung ist die Grenze Mechanismus/Domäne
(Spec §1.1): Sluice liefert die Muster-Sets als wählbare Profile, zieht aber keine
Domänenlogik in den Kern.

Konservative Deny-Muster: lieber false-positive (blockt zu viel) als ein Leck.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DenyPattern:
    """Ein Deny-Muster der Verifier-Engine (Spec §5).

    finding:   Text des Befunds im Blockier-Grund (und Audit, Spec §6).
    per_match: True → jeder Treffer wird einzeln als Befund gelistet (z. B. IPs);
               False → ein Sammel-Befund, sobald das Muster irgendwo greift.
    validate:  optionaler Zusatz-Check pro Treffer (z. B. echte IPv4-Validierung).
    """

    finding: str
    pattern: re.Pattern[str]
    per_match: bool = False
    validate: Callable[[str], bool] | None = None
    placeholder: str = "[redacted]"  # Ersatz beim Auto-Redigieren (strict-Modus, §3)


@dataclass(frozen=True)
class DetectorProfile:
    """Ein benanntes Muster-Set, das der Verifier lädt (Spec §5.1)."""

    name: str
    deny: tuple[DenyPattern, ...] = field(default_factory=tuple)


from sluice.detectors.code import PROFILE as _CODE  # noqa: E402
from sluice.detectors.financial import PROFILE as _FINANCIAL  # noqa: E402
from sluice.detectors.infra import PROFILE as _INFRA  # noqa: E402
from sluice.detectors.media import PROFILE as _MEDIA  # noqa: E402
from sluice.detectors.pii_de import PROFILE as _PII_DE  # noqa: E402

_PROFILES: dict[str, DetectorProfile] = {
    _INFRA.name: _INFRA,
    _CODE.name: _CODE,
    _MEDIA.name: _MEDIA,
    _FINANCIAL.name: _FINANCIAL,
    _PII_DE.name: _PII_DE,  # deutsche PII mit Prüfsummen (Rev. 12, §5.3)
}


def get_detector_profile(name: str) -> DetectorProfile | None:
    """Muster-Set per Name; None bei unbekanntem Profil (Verifier blockt dann fail-closed)."""
    return _PROFILES.get(name)


# Redaktions-Platzhalter für Wörterbuch-Treffer (Rev. 10). Der Hauptfall ist der
# personenbezogene Name; nicht-Namen werden ebenso sicher ersetzt, nur so etikettiert.
DICTIONARY_PLACEHOLDER = "[NAME]"


def build_dictionary_patterns(terms: Iterable[str]) -> tuple[DenyPattern, ...]:
    """Baut Deny-Muster aus konsument-deklarierten Wörterbuch-Termen (Spec §5.1, Rev. 10).

    Jeder Term wird **literal** (regex-escaped), **wortgrenzen-gebunden** und
    **case-insensitiv** gematcht — das deckt freie Namen/Adressen ab, die die generischen
    Regex-Muster (`infra`/`media`) prinzipbedingt nicht erkennen. Leere/whitespace-Terme
    werden übersprungen. Die *Liste* ist Domäne (Profil), das *Matching* ist Mechanismus.
    """
    patterns: list[DenyPattern] = []
    for term in terms:
        cleaned = term.strip()
        if not cleaned:
            continue
        patterns.append(
            DenyPattern(
                finding="Wörterbuch-Term erkannt",
                pattern=re.compile(rf"\b{re.escape(cleaned)}\b", re.IGNORECASE),
                placeholder=DICTIONARY_PLACEHOLDER,
            )
        )
    return tuple(patterns)
