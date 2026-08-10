"""Egress-Guard — orchestriert Gate → Modus → (Verifier) → Audit (Spec §2, Rev. 9).

Der EINZIGE Pfad nach außen. Jeder ausgehende Datenpfad aller Konsumenten läuft
durch diese Kette, bevor er die Kundengrenze überquert. Kein Modul ruft je direkt
nach außen.

Der Modus-Schalter (Rev. 9) zieht den Modus aus der Registry (§3); der Guard bleibt
darüber gleich. Was der `strict`-Default garantiert (§2, früher „drei Invarianten"):
1. Profil-Gate zuerst — kein Profil → nichts raus (Default-Deny, §4.3);
   `egress_enabled=false` → nichts raus, egal welcher Modus (§4.2).
2. Verifier — als Baustein, den der Modus komponiert (`enforce_verifier`, §5).
   `strict`/`generalizing`/`pseudonymizing`: fail-closed. `passthrough`: bewusst ohne (§2.1).
3. Audit — Detailgrad wählt der Betreiber (`off|metadata|full`, §6).

Herkunft: Tempers egress/guard.py, mit eingezogenem Modus-Aufruf (statt fest generalisierend).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from sluice.audit import AuditLog
from sluice.audit import egress_log as _default_audit
from sluice.errors import ModeUnavailableError
from sluice.policy import (
    Profile,
    check_egress_allowed,
    check_mode_allowed,
    check_provider_allowed,
)
from sluice.modes import EgressPayload, Mode, Sanitized, Scope, select_mode
from sluice.verifier import verify_no_identifiers

log = structlog.get_logger("sluice.guard")


@dataclass
class EgressOutcome:
    released: bool
    sanitized_text: str | None
    reason: str
    sanitized_messages: list[dict[str, Any]] | None = None
    # Rev. 12: unterscheidet die *Art* der Blockade. None = normale Policy-/Verifier-
    # Entscheidung; `mode_unavailable` = ein Modus konnte seine Zusage nicht einlösen
    # (z. B. NER-Dienst weg, §5.3). Beides blockiert — aber ein Ausfall darf beim
    # Aufrufer nicht als Policy-Ablehnung ankommen und im Rauschen untergehen.
    error_type: str | None = None


async def guarded_egress(
    *,
    profile: Profile | None,
    purpose: str,
    payload: EgressPayload,
    scope: Scope | None = None,
    provider_target: str | None = None,
    mode: Mode | None = None,
    audit: AuditLog | None = None,
) -> EgressOutcome:
    """Lässt Inhalt NUR durch, wenn Profil-Gate **und** Verifier zustimmen (Spec §2).

    profile:         das deklarierte Konsumenten-Profil; None → Default-Deny (§4.3).
    purpose:         wofür der Egress ist — muss in `profile.allowed_purposes` stehen.
    payload:         Roh-Text (nur Audit) + Egress-Kandidat (je nach Modus-Form).
    scope:           Mapping-Scope, nur für pseudonymizing relevant (§8).
    provider_target: Ziel-Provider; wird gegen die Profil-Allowlist geprüft (§4.1).
    mode:            Injektion für Tests; sonst per Profil gewählt (§3, der Schalter).
    audit:           Injektion für Tests; sonst das eine prozessweite egress_log (§6).
    """
    audit_log = audit if audit is not None else _default_audit
    mode_name = profile.mode if profile is not None else None

    def _blocked(
        reason: str, findings: tuple[str, ...] = (), *, error_type: str | None = None
    ) -> EgressOutcome:
        audit_log.append(
            profile=profile.name if profile is not None else None,
            purpose=purpose,
            mode=mode_name,
            released=False,
            reason=reason,
            before=payload.raw_text,
            after=None,
            provider_target=provider_target,
            verifier_findings=findings,
        )
        return EgressOutcome(
            released=False, sanitized_text=None, reason=reason, error_type=error_type
        )

    # 1. Profil-Gate (Invariante 1) — greift *vor* der Modus-Auswahl.
    decision = check_egress_allowed(profile, purpose)
    if not decision.allowed:
        return _blocked(decision.reason)
    assert profile is not None  # check_egress_allowed verweigert profile=None

    if provider_target is not None:
        provider_decision = check_provider_allowed(profile, provider_target)
        if not provider_decision.allowed:
            return _blocked(provider_decision.reason)

    # 2. Modus wählen (der Schalter zieht ihn aus der Registry, §3). select_mode hat
    #    keinen Egress-Effekt — es baut/cached nur die Instanz; forward() folgt erst nach
    #    der Allowlist-Prüfung.
    chosen = mode if mode is not None else select_mode(profile)

    # Modus-Allowlist (§4.1) — ein per Profil gesperrter Modus wird fail-closed abgewiesen,
    # auch wenn ein Request ihn wählt. Rev. 11: fail-open-Modi (ohne Verifier, §2.1) brauchen
    # explizites Opt-in in allowed_modes — die leere Allowlist erlaubt sie NICHT. Die Regel
    # greift generisch über chosen.enforce_verifier (§3), nicht am Namen `passthrough`.
    mode_decision = check_mode_allowed(
        profile, chosen.name, enforce_verifier=chosen.enforce_verifier
    )
    if not mode_decision.allowed:
        return _blocked(mode_decision.reason)

    # Fail-closed auf der Verfügbarkeits-Achse (Rev. 12, §5.3): kann ein Modus seine
    # Zusage gerade nicht einlösen — NER-Dienst weg, Timeout gerissen, Modellidentität
    # abweichend —, wird **blockiert**. Kein Rückfall auf eine schwächere Stufe: ein
    # Chokepoint, der bei Ausfall durchlässiger wird, ist kein Chokepoint. Der Guard
    # greift dabei generisch am Fehlertyp (§3), er kennt NER nicht namentlich.
    try:
        sanitized: Sanitized = await chosen.forward(payload, scope)
    except ModeUnavailableError as exc:
        log.error(
            "guard.mode_unavailable", profile=profile.name, mode=chosen.name, error=str(exc)
        )
        return _blocked(
            f"Modus '{chosen.name}' nicht verfügbar (fail-closed, §5.3): {exc}",
            error_type="mode_unavailable",
        )

    texts = sanitized.texts()
    if not texts:
        return _blocked("Kein Egress-Kandidat vorhanden (fail-closed).")

    # 3. Deterministischer Riegel — nur wenn der Modus ihn komponiert (Rev. 9, §5).
    #    `strict`/`generalizing`/`pseudonymizing`: fail-closed. `passthrough`: bewusst
    #    ohne Verifier — der Konsument trägt das Risiko (§2.1).
    if chosen.enforce_verifier:
        findings: list[str] = []
        for text in texts:
            verification = verify_no_identifiers(
                text, profile.detector_profile, dictionary_terms=profile.dictionary_terms
            )
            findings.extend(verification.findings)
        if findings:
            return _blocked(f"Verifier blockiert: {', '.join(findings)}", tuple(findings))

    # 4. Durchlass — protokollieren nach Betreiber-Audit-Level (Invariante-3-Verhalten, §6).
    audit_log.append(
        profile=profile.name,
        purpose=purpose,
        mode=chosen.name,
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
