"""Detektor-Profil `code` — wie `infra`, plus Code-spezifische Identifier (Spec §5.1).

Zusätzlich zu Tempers Infra-Set: interne Repo-/Package-Pfade (Git-Remotes,
`~/git/...`) und gesetzte Env-Var-Werte. Muster-Gerüst; Feinschliff ist
spätere Profil-Arbeit.
"""

from __future__ import annotations

import re

from sluice.detectors import DenyPattern, DetectorProfile
from sluice.detectors.infra import PROFILE as _INFRA

GIT_REMOTE = DenyPattern(
    finding="interne Git-Remote-/Repo-Referenz erkannt",
    pattern=re.compile(r"\b(?:git@|ssh://)[\w.@:/~-]+"),
)
REPO_PATH = DenyPattern(
    finding="lokaler Repo-Pfad erkannt",
    pattern=re.compile(r"(?:~|/home/[\w-]+)/git/[\w./-]+"),
)
ENV_VALUE = DenyPattern(
    finding="gesetzter Env-Var-Wert erkannt",
    pattern=re.compile(r"\b[A-Z][A-Z0-9_]{3,}\s*=\s*\S+"),
)

PROFILE = DetectorProfile(
    name="code",
    deny=_INFRA.deny + (GIT_REMOTE, REPO_PATH, ENV_VALUE),
)
