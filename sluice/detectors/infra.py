"""Detektor-Profil `infra` — Tempers heutiges Muster-Set, 1:1 portiert (Spec §5.1).

Herkunft: temper/egress/verifier.py. Die Muster sind bewusst konservativ:
lieber false-positive als ein Leck.
"""

from __future__ import annotations

import ipaddress
import re

from sluice.detectors import DenyPattern, DetectorProfile


def _looks_like_ip(token: str) -> bool:
    try:
        ipaddress.ip_address(token)
        return True
    except ValueError:
        return False


IPV4 = DenyPattern(
    finding="IP-Adresse",
    pattern=re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    per_match=True,
    validate=_looks_like_ip,
    placeholder="[IP]",
)
EMAIL = DenyPattern(
    finding="E-Mail-Adresse erkannt",
    pattern=re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    placeholder="[EMAIL]",
)
HOSTNAME_INTERNAL = DenyPattern(
    finding="interner Hostname erkannt",
    pattern=re.compile(r"\b(?:[a-z0-9-]+\.)+(?:internal|local|corp|lan|intra)\b", re.IGNORECASE),
    placeholder="[HOST]",
)
FQDN = DenyPattern(
    finding="FQDN erkannt",
    pattern=re.compile(r"\b(?:[a-z0-9-]+\.){2,}[a-z]{2,}\b", re.IGNORECASE),
    placeholder="[FQDN]",
)
PATH_USER = DenyPattern(
    finding="Benutzer-Pfad (/home/<user>) erkannt",
    pattern=re.compile(r"/home/[a-z0-9_-]+", re.IGNORECASE),
    placeholder="[PATH]",
)
SECRET = DenyPattern(
    finding="möglicher Secret-/Token-Wert erkannt",
    pattern=re.compile(r"(?i)\b(?:api[_-]?key|token|password|secret|bearer)\b\s*[:=]\s*\S+"),
    placeholder="[SECRET]",
)

PROFILE = DetectorProfile(
    name="infra",
    deny=(IPV4, EMAIL, HOSTNAME_INTERNAL, FQDN, PATH_USER, SECRET),
)
