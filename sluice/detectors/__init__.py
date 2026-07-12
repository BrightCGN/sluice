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
from collections.abc import Callable
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

_PROFILES: dict[str, DetectorProfile] = {
    _INFRA.name: _INFRA,
    _CODE.name: _CODE,
    _MEDIA.name: _MEDIA,
    _FINANCIAL.name: _FINANCIAL,
}


def get_detector_profile(name: str) -> DetectorProfile | None:
    """Muster-Set per Name; None bei unbekanntem Profil (Verifier blockt dann fail-closed)."""
    return _PROFILES.get(name)
