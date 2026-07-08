"""Verifier-Tests — KRITISCH: der Verifier lässt KEINEN Identifier durch (Spec §5).

Enthält Tempers Referenz-Suite (tests/test_egress_guard.py, Verifier-Teil) 1:1
gegen das `infra`-Profil, plus die Detektor-Profile aus Spec §5.1.
"""

from __future__ import annotations

import pytest

from sluice.verifier import verify_no_identifiers

# ---------- portiert aus Temper (Profil `infra` = Tempers heutiges Set) ----------


@pytest.mark.parametrize(
    "text",
    [
        "Node 10.0.3.14 zeigt Merge-Throttling",  # IP
        "Kontakt admin@kunde-bank.de bei Fragen",  # E-Mail
        "Host es-prod-01.corp.internal überlastet",  # interner Hostname
        "Config unter /home/richard/elastic.yml",  # Benutzer-Pfad
        "api_key = sk-supersecret123",  # Secret
    ],
)
def test_verifier_blocks_identifiers(text: str) -> None:
    result = verify_no_identifiers(text, "infra")
    assert not result.clean
    assert result.findings


def test_verifier_passes_generalized_lesson() -> None:
    # Generalisierte Lektion ohne Spezifika → sauber.
    text = "ES HOT-Tier-Merge-Throttling unter Ingest-Spike: Merge-Scheduler-Threads begrenzen."
    assert verify_no_identifiers(text, "infra").clean


# ---------- Detektor-Profile (Spec §5.1) ----------


def test_unknown_detector_profile_fails_closed() -> None:
    result = verify_no_identifiers("völlig harmloser Text", "gibt-es-nicht")
    assert not result.clean
    assert "unbekanntes Detektor-Profil" in result.findings[0]


def test_code_profile_blocks_repo_paths_and_env_values() -> None:
    assert not verify_no_identifiers("siehe ~/git/temper/src/main.py", "code").clean
    assert not verify_no_identifiers("mit DATABASE_URL=postgres://x gestartet", "code").clean
    assert not verify_no_identifiers("clone via git@github.com:acme/internal.git", "code").clean


def test_media_profile_blocks_share_paths_but_is_lighter() -> None:
    assert not verify_no_identifiers("liegt auf /mnt/nas/musik/album.flac", "media").clean
    # media hat kein FQDN-Muster — öffentliche Domains sind dort ok.
    assert verify_no_identifiers("siehe die Doku auf docs.beispiel.de", "media").clean


def test_financial_profile_blocks_iban_bic_account() -> None:
    assert not verify_no_identifiers("Überweisung an DE89 3704 0044 0532 0130 00", "financial").clean
    assert not verify_no_identifiers("BIC: COBADEFFXXX", "financial").clean
    assert not verify_no_identifiers("Kontonummer: 12345678", "financial").clean


def test_financial_profile_includes_infra_set() -> None:
    # strenger justiert: das komplette Infra-Set gilt mit.
    assert not verify_no_identifiers("Rückfragen an ceo@kunde-bank.de", "financial").clean
