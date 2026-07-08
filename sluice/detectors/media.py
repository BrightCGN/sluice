"""Detektor-Profil `media` — leichtes Set: Pfade, NAS-Hosts; kaum PII (Spec §5.1).

Für Konsumenten wie Crate (Playlist-Kuratierung): wenig personenbezogene Fläche,
aber lokale Infrastruktur (Share-Pfade, NAS-Hostnamen) bleibt drin.
"""

from __future__ import annotations

import re

from sluice.detectors import DenyPattern, DetectorProfile
from sluice.detectors.infra import EMAIL, HOSTNAME_INTERNAL, IPV4, PATH_USER

SHARE_PATH = DenyPattern(
    finding="NAS-/Share-Pfad erkannt",
    pattern=re.compile(r"\\\\[\w-]+\\[\w$./\\-]+|/(?:mnt|srv|media|volume\d*)/\S+"),
)

PROFILE = DetectorProfile(
    name="media",
    deny=(IPV4, EMAIL, HOSTNAME_INTERNAL, PATH_USER, SHARE_PATH),
)
