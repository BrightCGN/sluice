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
        payload=EgressPayload(raw_text="Melde 192.0.2.21 an max.mustermann@example.com"),
        audit=AuditLog(),
    )
    assert outcome.released is True
    assert "192.0.2.21" not in outcome.sanitized_text
    assert "example.com" not in outcome.sanitized_text
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
            raw_text="192.0.2.1",
            messages=[{"role": "user", "content": "ping 192.0.2.1"}],
        ),
        audit=AuditLog(),
    )
    assert outcome.released is True
    assert outcome.sanitized_messages is not None
    assert "192.0.2.1" not in outcome.sanitized_messages[0]["content"]
    assert "[IP]" in outcome.sanitized_messages[0]["content"]


# ---------- passthrough: kein Verifier, Konsument trägt das Risiko (§2.1) ----------


async def test_passthrough_lets_raw_through_unverified() -> None:
    raw = "Melde 192.0.2.21 an max.mustermann@example.com"
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
        payload=EgressPayload(raw_text="192.0.2.1"),
        audit=AuditLog(),
    )
    assert outcome.released is False


async def test_passthrough_never_default_no_profile_denies() -> None:
    # Default-Deny bleibt: ohne Profil geht nichts raus, erst recht kein passthrough.
    outcome = await guarded_egress(
        profile=None,
        purpose="x",
        payload=EgressPayload(raw_text="192.0.2.1"),
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
        payload=EgressPayload(raw_text="192.0.2.1"),
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
        payload=EgressPayload(raw_text="192.0.2.1"),
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


# ---------- Registry-Reihenfolge (Rev. 12) ----------


def test_register_mode_wirkt_auch_vor_dem_lazy_laden_der_builtins() -> None:
    """Der Erweiterungspunkt (§3) darf nicht an der Aufrufreihenfolge hängen.

    Registriert sich ein Dritter, *bevor* irgendetwas die Built-ins angefasst hat, würde
    die spätere Lazy-Ladung ihn ohne diese Absicherung still überschreiben — der Modus
    wäre je nach Importpfad da oder nicht. Der Test simuliert den frischen Zustand.
    """
    import sluice.modes as modes_module
    from sluice.modes import register_mode, registered_modes, select_mode

    saved_factories = dict(modes_module._MODE_FACTORIES)
    saved_loaded = modes_module._builtins_loaded
    saved_instances = dict(modes_module._instances)

    class ThirdPartyMode:
        reversible = False
        name = "dritt-modus"
        enforce_verifier = True

        async def forward(self, payload, scope):  # type: ignore[no-untyped-def]
            return None

    try:
        # Frischer Prozesszustand: nichts geladen, nichts registriert.
        modes_module._MODE_FACTORIES.clear()
        modes_module._instances.clear()
        modes_module._builtins_loaded = False

        register_mode("dritt-modus", lambda p: ThirdPartyMode())

        # Beides muss danach dastehen — der Dritt-Modus UND die Built-ins.
        assert "dritt-modus" in registered_modes()
        assert {"strict", "passthrough", "pii_regex", "pii_ner"} <= registered_modes()

        profile = Profile(name="p", mode="dritt-modus", allowed_purposes=("x",))
        assert select_mode(profile).name == "dritt-modus"
    finally:
        modes_module._MODE_FACTORIES.clear()
        modes_module._MODE_FACTORIES.update(saved_factories)
        modes_module._instances.clear()
        modes_module._instances.update(saved_instances)
        modes_module._builtins_loaded = saved_loaded


def test_registrierung_ueberschreibt_einen_gleichnamigen_builtin() -> None:
    """Ein Built-in ersetzbar zu machen ist der Sinn der Registry (§3)."""
    import sluice.modes as modes_module
    from sluice.modes import register_mode, select_mode

    original = modes_module._MODE_FACTORIES.get("strict")
    saved_instances = dict(modes_module._instances)

    class ErsatzStrict:
        reversible = False
        name = "strict"
        enforce_verifier = True

    try:
        register_mode("strict", lambda p: ErsatzStrict())
        modes_module._instances.clear()
        assert isinstance(select_mode(Profile(name="p", mode="strict")), ErsatzStrict)
    finally:
        if original is not None:
            modes_module._MODE_FACTORIES["strict"] = original
        modes_module._instances.clear()
        modes_module._instances.update(saved_instances)
