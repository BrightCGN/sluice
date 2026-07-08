"""Sluice — die eine gemeinsame Sanitisierungs-Boundary (docs/SLUICE-BOUNDARY-SPEC.md).

Jeder Egress läuft durch dieselbe Kette: Profil-Gate → Strategie → Verifier → Audit.
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
from sluice.strategies import (
    EgressPayload,
    Sanitized,
    SanitizationStrategy,
    Scope,
    select_strategy,
)
from sluice.verifier import VerificationResult, verify_no_identifiers

__all__ = [
    "AuditLog",
    "EgressDecision",
    "EgressLogEntry",
    "EgressOutcome",
    "EgressPayload",
    "Profile",
    "ReversibleConfig",
    "Sanitized",
    "SanitizationStrategy",
    "Scope",
    "VerificationResult",
    "check_egress_allowed",
    "check_provider_allowed",
    "egress_log",
    "guarded_egress",
    "load_profiles",
    "parse_profiles",
    "select_strategy",
    "verify_no_identifiers",
]

__version__ = "0.1.0"
