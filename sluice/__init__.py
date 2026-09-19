"""Sluice — die eine gemeinsame Sanitisierungs-Boundary (docs/SLUICE-BOUNDARY-SPEC.md).

Jeder Egress läuft durch dieselbe Kette: Profil-Gate → Modus → Verifier → Audit.
"""

from __future__ import annotations

from sluice.audit import AuditLog, EgressLogEntry, egress_log
from sluice.guard import EgressOutcome, guarded_egress
from sluice.policy import (
    EgressDecision,
    Profile,
    ReversibleConfig,
    check_egress_allowed,
    check_provider_allowed,
    load_profiles,
    parse_profiles,
)
from sluice.modes import (
    EgressPayload,
    Mode,
    Sanitized,
    Scope,
    select_mode,
)
from sluice.verifier import VerificationResult, verify_no_identifiers

__all__ = [
    "AuditLog",
    "EgressDecision",
    "EgressLogEntry",
    "EgressOutcome",
    "EgressPayload",
    "Mode",
    "Profile",
    "ReversibleConfig",
    "Sanitized",
    "Scope",
    "VerificationResult",
    "check_egress_allowed",
    "check_provider_allowed",
    "egress_log",
    "guarded_egress",
    "load_profiles",
    "parse_profiles",
    "select_mode",
    "verify_no_identifiers",
]

# 0.2.0 (19.09.2026): Die Paketversion stand seit dem ersten Tag auf 0.1.0,
# waehrend die Spec von Rev. 9 auf Rev. 16 gewachsen ist — Tool-Calling in
# allen Adaptern (Rev. 14/15), zweistufige PII-Erkennung (Rev. 12),
# Modell-Rotation samt Kapazitaets-Telemetrie (Rev. 16). Die Zahl holt das
# nach. Die inhaltliche Fortschreibung bleibt die REVISION in
# docs/SLUICE-BOUNDARY-SPEC.md; diese hier sagt nur, welcher Stand installiert
# ist (pip/venv), und muss zu pyproject.toml passen.
__version__ = "0.2.0"
