"""Detektor-Profil `financial` — IBAN/BIC, Kontonummern; strenger justiert (Spec §5.1).

Strengstes Profil (Bank-Tool, Spec §4.4): umfasst das komplette Infra-Set plus
Finanz-Identifier. Muster-Gerüst genügt für v1; Feinschliff (Beträge, Gegenparteien-
Namen, Genericity-Check §5.2) ist spätere Profil-Arbeit — bewusst post-v1.
"""

from __future__ import annotations

import re

from sluice.detectors import DenyPattern, DetectorProfile
from sluice.detectors.infra import PROFILE as _INFRA

IBAN = DenyPattern(
    finding="IBAN erkannt",
    pattern=re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,3})?\b"),
)
BIC = DenyPattern(
    finding="BIC erkannt",
    pattern=re.compile(r"\b[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b"),
)
ACCOUNT_NUMBER = DenyPattern(
    finding="Kontonummer erkannt",
    pattern=re.compile(r"(?i)\bkonto(?:nummer|[ -]?nr\.?)?\s*[:#]?\s*\d{6,12}\b"),
)

PROFILE = DetectorProfile(
    name="financial",
    deny=_INFRA.deny + (IBAN, BIC, ACCOUNT_NUMBER),
)
