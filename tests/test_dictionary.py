"""Profil-Wörterbuch-Tests (Spec §5.1, Rev. 10).

Konsument-deklarierte Literale (Namen/Adressen), die die generischen Regex-Muster nicht
erkennen: `strict` redigiert sie zu `[NAME]`, der Verifier blockt sie fail-closed in jedem
Modus. Herkunft: das `dictionary` aus PrismClaws/Crates anon, jetzt zentral im Profil.
"""

from __future__ import annotations

from sluice.audit import AuditLog
from sluice.guard import guarded_egress
from sluice.policy import Profile, parse_profiles
from sluice.modes import EgressPayload
from sluice.verifier import redact_identifiers, verify_no_identifiers

TERMS = ("Mustermann", "Musterstraße 12")


# ---------- Engine-Ebene (verifier) ----------


def test_verify_blocks_dictionary_term() -> None:
    # "Mustermann" fängt kein Regex-Muster — nur das Wörterbuch.
    result = verify_no_identifiers("Party bei Mustermann", "media", dictionary_terms=TERMS)
    assert result.clean is False
    assert any("Wörterbuch-Term" in f for f in result.findings)


def test_verify_case_insensitive_and_word_bounded() -> None:
    assert verify_no_identifiers("PARTY BEI MUSTERMANN", "media", dictionary_terms=TERMS).clean is False
    # Wortgrenze: "Mustermannson" ist NICHT der Term "Mustermann".
    assert verify_no_identifiers("DJ Mustermannson legt auf", "media", dictionary_terms=TERMS).clean is True


def test_verify_without_terms_is_backward_compatible() -> None:
    assert verify_no_identifiers("Party bei Mustermann", "media").clean is True


def test_redact_replaces_dictionary_term() -> None:
    out = redact_identifiers("Party bei Mustermann in Musterstraße 12", "media", dictionary_terms=TERMS)
    assert "Mustermann" not in out
    assert "Musterstraße 12" not in out
    assert out.count("[NAME]") == 2


# ---------- Guard-Integration ----------

STRICT = Profile(
    name="crate",
    mode="strict",
    allowed_purposes=("playlist_curation",),
    detector_profile="media",
    dictionary_terms=TERMS,
)
GENERALIZING = Profile(
    name="temper-like",
    mode="generalizing",
    allowed_purposes=("x",),
    detector_profile="media",
    dictionary_terms=TERMS,
)


async def test_strict_redacts_dictionary_term_end_to_end() -> None:
    outcome = await guarded_egress(
        profile=STRICT,
        purpose="playlist_curation",
        payload=EgressPayload(raw_text="90er-Party bei Mustermann"),
        audit=AuditLog(),
    )
    assert outcome.released is True
    assert "Mustermann" not in outcome.sanitized_text
    assert "[NAME]" in outcome.sanitized_text


async def test_generalizing_blocks_dictionary_term_fail_closed() -> None:
    # Ein Modus, der nicht redigiert: der Verifier blockt den Term trotzdem (Boden gilt für alle).
    outcome = await guarded_egress(
        profile=GENERALIZING,
        purpose="x",
        payload=EgressPayload(raw_text="Party bei Mustermann", generalized_text="Party bei Mustermann"),
        audit=AuditLog(),
    )
    assert outcome.released is False
    assert "Verifier blockiert" in outcome.reason


# ---------- Profil-Parsing ----------


def test_parse_dictionary_terms() -> None:
    profiles = parse_profiles(
        '[profile."crate"]\nmode = "strict"\ndetector_profile = "media"\n'
        'dictionary_terms = ["Mustermann", "Musterstraße 12"]\n'
    )
    assert profiles["crate"].dictionary_terms == ("Mustermann", "Musterstraße 12")


def test_dictionary_terms_default_empty() -> None:
    assert Profile(name="p").dictionary_terms == ()
