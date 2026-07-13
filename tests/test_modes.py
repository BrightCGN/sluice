"""Modus-Registry-Tests (Spec §2.1/§3/§4.1/§4.3, Rev. 9).

Deckt die neuen Modi und Schalter ab: `strict` (auto-redigierend, Default),
`passthrough` (kein Verifier, Opt-in), die `allowed_modes`-Allowlist und den
Dritt-Registrierungs-Erweiterungspunkt.
"""

from __future__ import annotations

from sluice.audit import AuditLog
from sluice.guard import guarded_egress
from sluice.policy import Profile
from sluice.modes import EgressPayload, is_registered_mode, registered_modes

STRICT = Profile(
    name="default-consumer",
    mode="strict",
    allowed_purposes=("x",),
    detector_profile="infra",
)
PASS = Profile(
    name="experimenter",
    mode="passthrough",
    allowed_purposes=("x",),
    detector_profile="infra",
    allowed_modes=("passthrough",),  # Rev. 11: fail-open-Modus braucht explizites Opt-in (§4.1)
)


# ---------- strict: auto-redigierend, der sichere Default (§3/§4.3) ----------


async def test_strict_auto_redacts_and_releases() -> None:
    outcome = await guarded_egress(
        profile=STRICT,
        purpose="x",
        payload=EgressPayload(raw_text="Melde 192.168.50.21 an r.cochius@gmail.com"),
        audit=AuditLog(),
    )
    assert outcome.released is True
    assert "192.168.50.21" not in outcome.sanitized_text
    assert "gmail" not in outcome.sanitized_text
    assert "[IP]" in outcome.sanitized_text
    assert "[EMAIL]" in outcome.sanitized_text


async def test_strict_is_the_default_mode() -> None:
    # Profil ohne `mode` → strict; roher Identifier wird auto-redigiert, nicht durchgelassen.
    default_profile = Profile(name="p", allowed_purposes=("x",), detector_profile="infra")
    assert default_profile.mode == "strict"
    outcome = await guarded_egress(
        profile=default_profile,
        purpose="x",
        payload=EgressPayload(raw_text="Host 10.0.0.1 down"),
        audit=AuditLog(),
    )
    assert outcome.released is True
    assert "10.0.0.1" not in outcome.sanitized_text


async def test_strict_redacts_proxy_messages() -> None:
    outcome = await guarded_egress(
        profile=STRICT,
        purpose="x",
        payload=EgressPayload(
            raw_text="192.168.1.1",
            messages=[{"role": "user", "content": "ping 192.168.1.1"}],
        ),
        audit=AuditLog(),
    )
    assert outcome.released is True
    assert outcome.sanitized_messages is not None
    assert "192.168.1.1" not in outcome.sanitized_messages[0]["content"]
    assert "[IP]" in outcome.sanitized_messages[0]["content"]


# ---------- passthrough: kein Verifier, Konsument trägt das Risiko (§2.1) ----------


async def test_passthrough_lets_raw_through_unverified() -> None:
    raw = "Melde 192.168.50.21 an r.cochius@gmail.com"
    outcome = await guarded_egress(
        profile=PASS,
        purpose="x",
        payload=EgressPayload(raw_text=raw),
        audit=AuditLog(),
    )
    assert outcome.released is True
    assert outcome.sanitized_text == raw  # unverändert — der Verifier greift bewusst nicht


async def test_passthrough_still_obeys_profile_gate() -> None:
    # §2.1: das Profil-Gate läuft VOR der Modus-Auswahl — souverän blockt auch passthrough.
    sovereign = Profile(name="sov", mode="passthrough", egress_enabled=False, allowed_purposes=("x",))
    outcome = await guarded_egress(
        profile=sovereign,
        purpose="x",
        payload=EgressPayload(raw_text="192.168.1.1"),
        audit=AuditLog(),
    )
    assert outcome.released is False


async def test_passthrough_never_default_no_profile_denies() -> None:
    # Default-Deny bleibt: ohne Profil geht nichts raus, erst recht kein passthrough.
    outcome = await guarded_egress(
        profile=None,
        purpose="x",
        payload=EgressPayload(raw_text="192.168.1.1"),
        audit=AuditLog(),
    )
    assert outcome.released is False


# ---------- allowed_modes-Allowlist (§4.1) ----------


async def test_allowed_modes_blocks_forbidden_mode() -> None:
    locked = Profile(
        name="locked",
        mode="passthrough",
        allowed_purposes=("x",),
        allowed_modes=("strict",),  # passthrough gesperrt
    )
    outcome = await guarded_egress(
        profile=locked,
        purpose="x",
        payload=EgressPayload(raw_text="192.168.1.1"),
        audit=AuditLog(),
    )
    assert outcome.released is False
    assert "allowed_modes" in outcome.reason


async def test_empty_allowed_modes_permits_verifier_modes() -> None:
    # Rev. 11: leere Allowlist erlaubt weiterhin ALLE verifizierenden Modi (sie leaken
    # nicht, §5). strict mit leerer allowed_modes läuft ganz normal durch.
    strict_open = Profile(
        name="open", mode="strict", allowed_purposes=("x",), detector_profile="infra"
    )
    assert strict_open.allowed_modes == ()
    outcome = await guarded_egress(
        profile=strict_open,
        purpose="x",
        payload=EgressPayload(raw_text="nichts sensibles"),
        audit=AuditLog(),
    )
    assert outcome.released is True


async def test_passthrough_needs_explicit_opt_in() -> None:
    # Rev. 11 (Kernänderung): fail-open-Modus mit LEERER allowed_modes ⇒ fail-closed.
    # „Vergessen der Allowlist = zu", nicht „offen" (§4.1/§4.3).
    forgot = Profile(
        name="forgot",
        mode="passthrough",
        allowed_purposes=("x",),
        detector_profile="infra",
        allowed_modes=(),  # Betreiber hat passthrough NICHT freigegeben
    )
    outcome = await guarded_egress(
        profile=forgot,
        purpose="x",
        payload=EgressPayload(raw_text="192.168.1.1"),
        audit=AuditLog(),
    )
    assert outcome.released is False
    assert "Opt-in" in outcome.reason


# ---------- Registry-Erweiterungspunkt (§3) ----------


def test_builtin_modes_are_registered() -> None:
    modes = registered_modes()
    assert {"strict", "passthrough", "generalizing", "pseudonymizing"} <= modes
    assert is_registered_mode("strict")
    assert not is_registered_mode("nonexistent-mode")
