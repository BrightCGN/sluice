"""Regex-Stufe `pii_de` — Prüfziffernverfahren und Muster (Spec §5.1/§5.3, Rev. 12).

Der Kern dieser Suite: die Prüfsummen sind das, was der Regex-Stufe ihre Präzision gibt
und sie damit unersetzbar durch ein Modell macht (§5.3). Ein kaputter Validator wäre
nicht „etwas ungenauer", sondern würde die Begründung der ganzen Zweistufigkeit
aushebeln — deshalb hier ausdrücklich auch die **Negativ**-Fälle.
"""

from __future__ import annotations

from sluice.detectors.pii_de import (
    validate_iban,
    validate_ipv6,
    validate_kvnr,
    validate_luhn,
    validate_steuer_id,
    validate_svnr,
)
from sluice.spans import detect_regex_spans


# ---------- Prüfziffernverfahren (§5.3) ----------


def test_iban_mod97() -> None:
    assert validate_iban("DE89370400440532013000") is True
    assert validate_iban("DE89 3704 0044 0532 0130 00") is True  # Leerzeichen-Gruppierung
    assert validate_iban("AT611904300234573201") is True
    assert validate_iban("CH9300762011623852957") is True
    # Eine einzelne falsche Prüfziffer muss durchfallen — sonst ist Mod-97 wirkungslos.
    assert validate_iban("DE89370400440532013001") is False
    # Länderspezifische Länge: DE hat 22 Stellen, 21 ist keine IBAN.
    assert validate_iban("DE8937040044053201300") is False


def test_steuer_id_pruefziffer_und_strukturregel() -> None:
    assert validate_steuer_id("86095742719") is True
    assert validate_steuer_id("47036892816") is True
    assert validate_steuer_id("86095742710") is False  # falsche Prüfziffer
    assert validate_steuer_id("02476291358") is False  # führende 0 ist unzulässig
    # Amtliche Strukturregel: dreifach vorkommende Ziffer darf nicht direkt
    # aufeinanderfolgen. Prüfziffer stimmt hier, die Struktur nicht.
    assert validate_steuer_id("65929970489") is False


def test_svnr_pruefziffer() -> None:
    assert validate_svnr("65170839J003") is True
    assert validate_svnr("65170839J004") is False
    assert validate_svnr("65170839X003") is False  # Buchstabe geht in die Prüfziffer ein


def test_kvnr_pruefziffer() -> None:
    assert validate_kvnr("A123456780") is True
    assert validate_kvnr("A123456781") is False


def test_luhn() -> None:
    assert validate_luhn("4111111111111111") is True
    assert validate_luhn("5500 0000 0000 0004") is True
    assert validate_luhn("4111111111111112") is False


def test_ipv6() -> None:
    assert validate_ipv6("2001:0db8:85a3::8a2e:0370:7334") is True
    assert validate_ipv6("2001:0db8:85a3::8a2e:0370:733z") is False


# ---------- Muster über den Text (§5.1) ----------


def test_alle_geforderten_typen_werden_erkannt() -> None:
    """Die in der Anforderung namentlich genannten deutschen Ausprägungen."""
    text = (
        "IBAN DE89 3704 0044 0532 0130 00, Steuer-ID 86095742719, "
        "SVNR 65170839J003, KVNR A123456780, Karte 4111 1111 1111 1111, "
        "Mail max.mustermann@example.com, IPv4 192.0.2.21, "
        "IPv6 2001:0db8:85a3::8a2e:0370:7334, MAC 00:1B:44:11:3A:B7, "
        "KFZ K-AB 1234, Telefon 0221 4710815"
    )
    labels = {s.label for s in detect_regex_spans(text, "pii_de")}
    for expected in (
        "[IBAN]",
        "[STEUER_ID]",
        "[SVNR]",
        "[KVNR]",
        "[CREDITCARD]",
        "[EMAIL]",
        "[IP]",
        "[IPV6]",
        "[MAC]",
        "[KFZ]",
        "[PHONE]",
    ):
        assert expected in labels, f"{expected} fehlt in {sorted(labels)}"


def test_ungueltige_pruefsummen_erzeugen_keinen_span() -> None:
    """Was die Prüfsumme nicht besteht, ist kein Treffer dieses Typs.

    Wichtig für die Arbeitsteilung (§5.3): erst diese Präzision rechtfertigt, dass die
    Regex-Stufe bei Überlappung Vorrang vor dem Modell bekommt.
    """
    spans = detect_regex_spans("Rechnung DE89370400440532013001 vom Montag", "pii_de")
    assert not [s for s in spans if s.label == "[IBAN]"]


def test_spans_sind_offsets_in_den_originaltext() -> None:
    text = "Bitte an max.mustermann@example.com senden"
    (span,) = [s for s in detect_regex_spans(text, "pii_de") if s.label == "[EMAIL]"]
    assert text[span.start : span.end] == "max.mustermann@example.com"
