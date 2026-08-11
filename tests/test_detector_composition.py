"""Zusammengelegte Detektor-Profile (Spec §5.1, Rev. 13).

Bis Rev. 12 nahm `detector_profile` **einen** Namen. Das zwang zu einer Wahl, die man
nicht gewinnen kann: `media` kennt NAS-Pfade und interne Hostnamen, aber keine IBAN;
`pii_de` kennt IBAN, Steuer-ID und SVNR mit Prüfziffern, aber keine NAS-Pfade. Wer sich
entscheiden muss, tauscht Schutz in der eigenen Domäne gegen Schutz in einer fremden —
und ein Profil für Korrespondenz oder Tickets braucht beides gleichzeitig.

Rev. 13 legt mehrere Muster-Sets zur **Vereinigungsmenge** zusammen und führt sie unter
einem kanonischen Namen (`media+pii_de`). Nach außen bleibt es *ein* Name: Verifier,
Span-Erkennung und Anonymisierungs-Identität arbeiten unverändert weiter.
"""

from __future__ import annotations

import pytest

from sluice.detectors import (
    UnknownDetectorProfile,
    get_detector_profile,
    merge_detector_profiles,
)
from sluice.identity import build_identity
from sluice.policy import parse_profiles
from sluice.spans import detect_regex_spans

# Enthält absichtlich beides: einen NAS-Pfad (nur `media`) und eine gültige IBAN
# (nur `pii_de`, mit Mod-97-Prüfung). Genau der Fall, an dem die Einzelwahl scheitert.
GEMISCHT = "Playlist unter /mnt/musik/archiv, Abrechnung an DE89 3704 0044 0532 0130 00."


def profil_toml(detector: str) -> str:
    return f"""
    [profile."x"]
    mode = "strict"
    egress_enabled = true
    allowed_purposes = ["t"]
    provider_allowlist = ["anthropic"]
    detector_profile = {detector}
    """


# ---- Zusammenlegen ------------------------------------------------------------------


def test_ein_name_bleibt_unveraendert() -> None:
    """Rückwärtskompatibel: bestehende Profile behalten Namen *und* Identitäts-Digest."""
    assert merge_detector_profiles(["infra"]) == "infra"


def test_mehrere_namen_ergeben_kanonischen_namen() -> None:
    assert merge_detector_profiles(["media", "pii_de"]) == "media+pii_de"


def test_reihenfolge_egal() -> None:
    """Die Vereinigung ist ordnungsunabhängig — eine Umordnung darf den Digest nicht bewegen."""
    assert merge_detector_profiles(["pii_de", "media"]) == merge_detector_profiles(
        ["media", "pii_de"]
    )


def test_dubletten_werden_entfernt() -> None:
    """`media` und `pii_de` führen beide E-Mail und IP — doppelt bläht nur den Befund auf."""
    name = merge_detector_profiles(["media", "pii_de"])
    zusammen = len(get_detector_profile(name).deny)
    roh = len(get_detector_profile("media").deny) + len(get_detector_profile("pii_de").deny)
    assert zusammen < roh


def test_vereinigung_enthaelt_beide_seiten() -> None:
    name = merge_detector_profiles(["media", "pii_de"])
    befunde = {d.finding for d in get_detector_profile(name).deny}
    assert "NAS-/Share-Pfad erkannt" in befunde
    assert "IBAN erkannt (Mod-97 geprüft)" in befunde


def test_unbekannter_name_ist_ein_fehler() -> None:
    with pytest.raises(UnknownDetectorProfile, match="tippfehler"):
        merge_detector_profiles(["media", "tippfehler"])


def test_leere_liste_ist_ein_fehler() -> None:
    with pytest.raises(UnknownDetectorProfile):
        merge_detector_profiles([])


# ---- Wirkung: fängt die Vereinigung wirklich beides? ---------------------------------


def test_einzelnes_profil_verfehlt_die_jeweils_andere_seite() -> None:
    """Die Ausgangslage, die Rev. 13 auflöst — als Test festgehalten."""
    nur_media = {s.finding for s in detect_regex_spans(GEMISCHT, "media")}
    nur_pii = {s.finding for s in detect_regex_spans(GEMISCHT, "pii_de")}

    assert any("NAS" in f for f in nur_media)
    assert not any("IBAN" in f for f in nur_media), "media sollte keine IBAN kennen"

    assert any("IBAN" in f for f in nur_pii)
    assert not any("NAS" in f for f in nur_pii), "pii_de sollte keinen NAS-Pfad kennen"


def test_vereinigung_faengt_beides() -> None:
    name = merge_detector_profiles(["media", "pii_de"])
    befunde = {s.finding for s in detect_regex_spans(GEMISCHT, name)}
    assert any("NAS" in f for f in befunde)
    assert any("IBAN" in f for f in befunde)


# ---- Profil-Schema -------------------------------------------------------------------


def test_string_im_profil_funktioniert_weiter() -> None:
    profile = parse_profiles(profil_toml('"media"'))["x"]
    assert profile.detector_profile == "media"


def test_liste_im_profil_wird_zusammengelegt() -> None:
    profile = parse_profiles(profil_toml('["media", "pii_de"]'))["x"]
    assert profile.detector_profile == "media+pii_de"


def test_tippfehler_bricht_das_laden_ab() -> None:
    """Ab Rev. 13 ein Ladefehler statt eines stillen Durchlaufs mit späterem Block.

    Vorher lud ein Zeichendreher klaglos, und der Verifier blockte erst zur Laufzeit mit
    „unbekanntes Detektor-Profil" — fail-closed, aber als Fehlerbild irreführend.
    """
    with pytest.raises(ValueError, match="tippfehler"):
        parse_profiles(profil_toml('"tippfehler"'))


def test_tippfehler_in_der_liste_bricht_ebenfalls_ab() -> None:
    with pytest.raises(ValueError, match="tippfehler"):
        parse_profiles(profil_toml('["media", "tippfehler"]'))


def test_nicht_string_eintraege_werden_abgelehnt() -> None:
    with pytest.raises(ValueError, match="Strings"):
        parse_profiles(profil_toml("[\"media\", 42]"))


# ---- Anonymisierungs-Identität --------------------------------------------------------


def test_zusammensetzung_steht_in_der_identitaet() -> None:
    """Im Audit soll ablesbar sein, *womit* geprüft wurde — nicht nur, dass geprüft wurde."""
    identitaet = build_identity(mode="strict", detector_profile="media+pii_de")
    assert identitaet.as_dict()["detector_profile"] == "media+pii_de"


def test_andere_zusammensetzung_aendert_den_digest() -> None:
    einzeln = build_identity(mode="strict", detector_profile="media").digest()
    gemischt = build_identity(mode="strict", detector_profile="media+pii_de").digest()
    assert einzeln != gemischt


def test_digest_eines_einzelprofils_bleibt_stabil() -> None:
    """Bestandsprofile dürfen durch Rev. 13 ihren Fingerabdruck nicht wechseln."""
    profile = parse_profiles(profil_toml('"infra"'))["x"]
    assert (
        build_identity(mode="strict", detector_profile=profile.detector_profile).digest()
        == build_identity(mode="strict", detector_profile="infra").digest()
    )
