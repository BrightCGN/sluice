"""Egress-Guard — orchestriert Gate → Strategie → Verifier → Audit (Spec §2).

Der EINZIGE Pfad nach außen. Jeder ausgehende Datenpfad aller Konsumenten läuft
durch diese Kette, bevor er die Kundengrenze überquert. Kein Modul ruft je direkt
nach außen.

Die drei Invarianten, die der Strategie-Schalter NIE verändert (Spec §2):
1. Profil-Gate zuerst — kein Profil → nichts raus (Default-Deny, §4.3);
   `egress_enabled=false` → nichts raus, egal welche Strategie (§4.2).
2. Verifier gleich streng in beiden Modi — die Schicht *unter* der Strategie (§5).
3. Audit bei jedem Durchlass — append-only, released *und* blocked (§6).

Herkunft: Tempers egress/guard.py, mit eingezogenem Strategie-Aufruf
(statt fest generalisierend).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from sluice.audit import AuditLog
from sluice.audit import egress_log as _default_audit
from sluice.policy import Profile, check_egress_allowed, check_provider_allowed
from sluice.strategies import EgressPayload, Sanitized, SanitizationStrategy, Scope, select_strategy
from sluice.verifier import verify_no_identifiers

log = structlog.get_logger("sluice.guard")


@dataclass
class EgressOutcome:
    released: bool
    sanitized_text: str | None
    reason: str
    sanitized_messages: list[dict[str, Any]] | None = None


async def guarded_egress(
    *,
    profile: Profile | None,
    purpose: str,
    payload: EgressPayload,
    scope: Scope | None = None,
    provider_target: str | None = None,
    strategy: SanitizationStrategy | None = None,
    audit: AuditLog | None = None,
) -> EgressOutcome:
    """Lässt Inhalt NUR durch, wenn Profil-Gate **und** Verifier zustimmen (Spec §2).

    profile:         das deklarierte Konsumenten-Profil; None → Default-Deny (§4.3).
    purpose:         wofür der Egress ist — muss in `profile.allowed_purposes` stehen.
    payload:         Roh-Text (nur Audit) + Egress-Kandidat (je nach Strategie-Form).
    scope:           Mapping-Scope, nur für pseudonymizing relevant (§8).
    provider_target: Ziel-Provider; wird gegen die Profil-Allowlist geprüft (§4.1).
    strategy:        Injektion für Tests; sonst per Profil gewählt (§2, der Schalter).
    audit:           Injektion für Tests; sonst das eine prozessweite egress_log (§6).
    """
    audit_log = audit if audit is not None else _default_audit
    strategy_name = profile.strategy if profile is not None else None

    def _blocked(reason: str, findings: tuple[str, ...] = ()) -> EgressOutcome:
        audit_log.append(
            profile=profile.name if profile is not None else None,
            purpose=purpose,
            strategy=strategy_name,
            released=False,
            reason=reason,
            before=payload.raw_text,
            after=None,
            provider_target=provider_target,
            verifier_findings=findings,
        )
        return EgressOutcome(released=False, sanitized_text=None, reason=reason)

    # 1. Profil-Gate (Invariante 1) — greift *vor* der Strategie-Auswahl.
    decision = check_egress_allowed(profile, purpose)
    if not decision.allowed:
        return _blocked(decision.reason)
    assert profile is not None  # check_egress_allowed verweigert profile=None

    if provider_target is not None:
        provider_decision = check_provider_allowed(profile, provider_target)
        if not provider_decision.allowed:
            return _blocked(provider_decision.reason)

    # 2. Strategie (der Schalter tauscht nur dieses Objekt, §2).
    chosen = strategy if strategy is not None else select_strategy(profile)
    sanitized: Sanitized = await chosen.forward(payload, scope)

    texts = sanitized.texts()
    if not texts:
        return _blocked("Kein Egress-Kandidat vorhanden (fail-closed).")

    # 3. Harter, deterministischer Riegel — gleich streng in BEIDEN Modi (Invariante 2).
    findings: list[str] = []
    for text in texts:
        verification = verify_no_identifiers(text, profile.detector_profile)
        findings.extend(verification.findings)
    if findings:
        return _blocked(f"Verifier blockiert: {', '.join(findings)}", tuple(findings))

    # 4. Durchlass — append-only protokollieren (Invariante 3, reviewbares Vorher/Nachher).
    audit_log.append(
        profile=profile.name,
        purpose=purpose,
        strategy=chosen.name,
        released=True,
        reason="clean",
        before=payload.raw_text,
        after="\n".join(texts),
        provider_target=provider_target,
    )
    return EgressOutcome(
        released=True,
        sanitized_text=sanitized.text,
        reason="clean",
        sanitized_messages=sanitized.messages,
    )
