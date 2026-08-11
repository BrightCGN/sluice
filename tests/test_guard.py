"""Guard-Tests — die drei Invarianten (Spec §2) und die Pflicht-Fälle der DoD.

Enthält Tempers Referenz-Test „souveränes Profil blockt alles" in portierter Form.
"""

from __future__ import annotations

from sluice.audit import AuditLog
from sluice.guard import guarded_egress
from sluice.policy import Profile, ReversibleConfig
from sluice.modes import EgressPayload, Scope

TEMPER = Profile(
    name="temper",
    mode="generalizing",
    egress_enabled=True,
    allowed_purposes=("promotion_upload", "external_escalation"),
    provider_allowlist=("claude", "gemini"),
    detector_profile="infra",
)

AIDER = Profile(
    name="aider-code",
    mode="pseudonymizing",
    egress_enabled=True,
    allowed_purposes=("code_completion",),
    provider_allowlist=("claude",),
    detector_profile="infra",
    reversible=ReversibleConfig(),
)

SOVEREIGN = Profile(
    name="sovereign",
    mode="generalizing",
    egress_enabled=False,
    allowed_purposes=("promotion_upload",),
)


# ---------- Pflicht-Fall: Default-Deny (§4.3) ----------


async def test_default_deny_no_profile_releases_nothing() -> None:
    audit = AuditLog()
    outcome = await guarded_egress(
        profile=None,
        purpose="external_escalation",
        payload=EgressPayload(raw_text="x", generalized_text="vollkommen sauber"),
        audit=audit,
    )
    assert outcome.released is False
    assert outcome.sanitized_text is None
    assert "Default-Deny" in outcome.reason


# ---------- Pflicht-Fall: souveränes Profil (§4.2) — portiert aus Temper ----------


async def test_sovereign_profile_blocks_all_egress() -> None:
    # Souveränes Profil: Egress technisch zu, selbst sauberer Text geht nicht raus.
    outcome = await guarded_egress(
        profile=SOVEREIGN,
        purpose="promotion_upload",
        payload=EgressPayload(
            raw_text="irgendwas",
            generalized_text="vollkommen generische, saubere Lektion",
        ),
        audit=AuditLog(),
    )
    assert outcome.released is False
    assert "air-gapped" in outcome.reason or "Egress" in outcome.reason


# ---------- Pflicht-Fall: Verifier gleich streng in BEIDEN Modi (§2, Invariante 2) ----------


async def test_verifier_equally_strict_in_both_modes() -> None:
    """Derselbe roh durchgeschmuggelte Identifier wird in beiden Modi blockiert.

    Der Hostname `es-prod-01.corp.internal` liegt außerhalb der Detektions-Muster der
    pseudonymisierenden Modus — das Mapping übersieht ihn. Genau dann muss der
    Verifier darunter greifen: „reversibel" ist kein Grund, den Riegel zu lockern.
    """
    smuggled = "Host es-prod-01.corp.internal überlastet"

    generalizing = await guarded_egress(
        profile=TEMPER,
        purpose="external_escalation",
        payload=EgressPayload(raw_text=smuggled, generalized_text=smuggled),
        audit=AuditLog(),
    )
    pseudonymizing = await guarded_egress(
        profile=AIDER,
        purpose="code_completion",
        payload=EgressPayload(raw_text=smuggled),
        scope=Scope("s1"),
        audit=AuditLog(),
    )

    assert generalizing.released is False
    assert pseudonymizing.released is False
    assert "Verifier blockiert" in generalizing.reason
    assert "Verifier blockiert" in pseudonymizing.reason


async def test_pseudonymized_output_passes_verifier() -> None:
    # Was das Mapping erkennt (IP, E-Mail), geht nur als Pseudonym raus → Verifier zufrieden.
    outcome = await guarded_egress(
        profile=AIDER,
        purpose="code_completion",
        payload=EgressPayload(raw_text="Melde 192.0.2.21 an max.mustermann@example.com"),
        scope=Scope("s1"),
        audit=AuditLog(),
    )
    assert outcome.released is True
    assert "192.168" not in outcome.sanitized_text
    assert "example.com" not in outcome.sanitized_text
    assert "⟦IP_1⟧" in outcome.sanitized_text


async def test_generalizing_clean_text_released() -> None:
    outcome = await guarded_egress(
        profile=TEMPER,
        purpose="promotion_upload",
        payload=EgressPayload(
            raw_text="Node 10.0.3.14 zeigt Merge-Throttling",
            generalized_text="HOT-Tier-Merge-Throttling unter Ingest-Spike: Threads begrenzen.",
        ),
        audit=AuditLog(),
    )
    assert outcome.released is True
    assert outcome.sanitized_text.startswith("HOT-Tier")


# ---------- Provider-Allowlist (§4.1) ----------


async def test_provider_not_in_allowlist_is_blocked() -> None:
    outcome = await guarded_egress(
        profile=TEMPER,
        purpose="promotion_upload",
        payload=EgressPayload(raw_text="x", generalized_text="sauber"),
        provider_target="openai",
        audit=AuditLog(),
    )
    assert outcome.released is False
    assert "Allowlist" in outcome.reason


# ---------- Invariante 3: Audit bei JEDEM Durchlass (§6) ----------


async def test_audit_written_for_release_and_block() -> None:
    # `full`-Level: reviewbares Vorher/Nachher wird persistiert (§6, Rev. 9).
    audit = AuditLog(level="full")

    await guarded_egress(
        profile=TEMPER,
        purpose="promotion_upload",
        payload=EgressPayload(raw_text="Node 10.0.3.14", generalized_text="sauber generell"),
        audit=audit,
    )
    await guarded_egress(
        profile=TEMPER,
        purpose="promotion_upload",
        payload=EgressPayload(raw_text="Node 10.0.3.14", generalized_text="leckt 10.0.3.14"),
        audit=audit,
    )

    assert len(audit.entries) == 2
    released, blocked = audit.entries
    assert released.released is True
    assert released.before == "Node 10.0.3.14"  # reviewbares Vorher/Nachher
    assert released.after == "sauber generell"
    assert blocked.released is False
    assert blocked.after is None
    assert blocked.verifier_findings  # Befunde landen im Audit


async def test_audit_streams_isolated_per_profile() -> None:
    # Mandanten-Isolation (§4.4): getrennte Sicht pro Profil.
    audit = AuditLog()
    await guarded_egress(
        profile=TEMPER,
        purpose="promotion_upload",
        payload=EgressPayload(raw_text="x", generalized_text="sauber"),
        audit=audit,
    )
    assert len(audit.for_profile("temper")) == 1
    assert len(audit.for_profile("crate")) == 0
