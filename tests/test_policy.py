"""Profil-Schema- und Profil-Gate-Tests (Spec §4)."""

from __future__ import annotations

import pytest

from sluice.policy import (
    Profile,
    check_egress_allowed,
    check_provider_allowed,
    parse_profiles,
)

# Das Beispiel-Schema aus Spec §4, wörtlich.
SPEC_TOML = """
[profile."temper"]
strategy            = "generalizing"
egress_enabled      = true
allowed_purposes    = ["external_escalation", "promotion_upload"]
provider_allowlist  = ["claude", "gemini"]
detector_profile    = "infra"

[profile."aider-code"]
strategy            = "pseudonymizing"
egress_enabled      = true
allowed_purposes    = ["code_completion"]
provider_allowlist  = ["claude"]
detector_profile    = "code"
  [profile."aider-code".reversible]
  scope             = "session"
  ttl_seconds       = 3600
  storage           = "memory"

[profile."sovereign"]
strategy            = "generalizing"
egress_enabled      = false
allowed_purposes    = []
provider_allowlist  = []
detector_profile    = "infra"
"""


def test_parse_spec_schema() -> None:
    profiles = parse_profiles(SPEC_TOML)
    temper = profiles["temper"]
    assert temper.strategy == "generalizing"
    assert temper.allowed_purposes == ("external_escalation", "promotion_upload")
    assert temper.reversible is None

    aider = profiles["aider-code"]
    assert aider.strategy == "pseudonymizing"
    assert aider.reversible is not None
    assert aider.reversible.scope == "session"
    assert aider.reversible.ttl_seconds == 3600
    assert aider.reversible.storage == "memory"


def test_parse_rejects_unknown_mode() -> None:
    # `strategy` bleibt Parse-Alias auf `mode` (Rev. 9); unbekannter Wert ⇒ fail-closed.
    with pytest.raises(ValueError, match="unbekannter Modus"):
        parse_profiles('[profile."x"]\nstrategy = "cleverhack"\n')
    with pytest.raises(ValueError, match="unbekannter Modus"):
        parse_profiles('[profile."x"]\nmode = "cleverhack"\n')


def test_parse_rejects_persistent_storage_v1() -> None:
    with pytest.raises(ValueError, match="storage"):
        parse_profiles(
            '[profile."x"]\nstrategy = "pseudonymizing"\n'
            '[profile."x".reversible]\nstorage = "persistent"\n'
        )


def test_default_deny_without_profile() -> None:
    decision = check_egress_allowed(None, "external_escalation")
    assert not decision.allowed
    assert "Default-Deny" in decision.reason


def test_sovereign_profile_denies_everything() -> None:
    sovereign = parse_profiles(SPEC_TOML)["sovereign"]
    decision = check_egress_allowed(sovereign, "external_escalation")
    assert not decision.allowed
    assert "air-gapped" in decision.reason or "Egress" in decision.reason


def test_purpose_must_be_allowed() -> None:
    temper = parse_profiles(SPEC_TOML)["temper"]
    assert check_egress_allowed(temper, "external_escalation").allowed
    assert not check_egress_allowed(temper, "code_completion").allowed


def test_provider_allowlist() -> None:
    temper = parse_profiles(SPEC_TOML)["temper"]
    assert check_provider_allowed(temper, "claude").allowed
    assert not check_provider_allowed(temper, "openai").allowed


def test_parse_allowed_modes() -> None:
    profiles = parse_profiles(
        '[profile."locked"]\nmode = "strict"\nallowed_modes = ["strict", "passthrough"]\n'
    )
    assert profiles["locked"].allowed_modes == ("strict", "passthrough")


def test_parse_rejects_unknown_allowed_mode() -> None:
    with pytest.raises(ValueError, match="allowed_modes"):
        parse_profiles('[profile."x"]\nallowed_modes = ["nope"]\n')


def test_default_mode_is_strict() -> None:
    # Rev. 9 (Spec §4.3, safety first): fehlt `mode`, gilt der sichere Default `strict`
    # (auto-redigierend, Verifier fail-closed) — nie `passthrough`, nie geerbt.
    assert Profile(name="neu").mode == "strict"
    assert Profile(name="neu").strategy == "strict"  # Lese-Alias
    assert parse_profiles('[profile."neu"]\negress_enabled = true\n')["neu"].mode == "strict"
