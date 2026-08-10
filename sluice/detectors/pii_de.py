"""Detektor-Profil `pii_de` — deutsche PII mit Prüfsummen-Validierung (Spec §5.1/§5.3, Rev. 12).

Die **Regex-Stufe** der Modi `pii_regex`/`pii_ner` (§3). Zuständig für alles, was eine
*feste, prüfbare Form* hat — und genau dafür ist sie dem Modell überlegen: eine IBAN mit
bestandener Mod-97-Prüfung *ist* eine IBAN, da rät nichts. Deshalb ersetzt das NER-Modell
diese Stufe nie, es ergänzt sie nur (§5.3).

Jedes Muster mit Prüfziffernverfahren setzt `per_match=True` — **zwingend**: ohne das
wertet `verify_no_identifiers` den `validate`-Callback gar nicht aus (§5) und würde jeden
Treffer roh als Befund führen. Alle Gruppen sind `(?:…)`, damit `findall` in der
Verifier-Engine den vollen Treffer liefert und nicht die Gruppen.

Die Muster sind bewusst **recall-orientiert** (§5.4): lieber ein übermaskierter Treffer
als ein durchgelassener Identifier. Wo eine Prüfsumme existiert, hebt sie die Präzision
ohne Recall-Verlust wieder an.
"""

from __future__ import annotations

import ipaddress
import re

from sluice.detectors import DenyPattern, DetectorProfile
from sluice.detectors.infra import EMAIL, IPV4, SECRET

# ---- Prüfziffernverfahren ------------------------------------------------------------


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def validate_iban(value: str) -> bool:
    """IBAN nach ISO 13616: Mod-97-10 == 1, plus länderspezifische Länge (§5.3).

    Die Längenprüfung ist der eigentliche Präzisionsgewinn — Mod-97 allein passiert
    zufällig etwa jede 97. Zeichenkette der richtigen Form.
    """
    compact = re.sub(r"\s", "", value).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", compact):
        return False
    expected = IBAN_LENGTHS.get(compact[:2])
    if expected is not None and len(compact) != expected:
        return False
    rearranged = compact[4:] + compact[:4]
    numeric = "".join(
        str(ord(ch) - 55) if ch.isalpha() else ch for ch in rearranged
    )
    return int(numeric) % 97 == 1


# Nur die Länder, die im Zielbetrieb realistisch vorkommen; unbekannte Präfixe werden
# allein über Mod-97 geprüft (fail-open auf der Längen-Achse, nie auf der Prüfsummen-Achse).
IBAN_LENGTHS: dict[str, int] = {
    "DE": 22, "AT": 20, "CH": 21, "LI": 21, "LU": 20, "NL": 18, "BE": 16,
    "FR": 27, "IT": 27, "ES": 24, "PL": 28, "CZ": 24, "DK": 18, "GB": 22,
}


def validate_steuer_id(value: str) -> bool:
    """Steuerliche Identifikationsnummer (IdNr, §139b AO): 11 Ziffern, ISO 7064 MOD 11,10.

    Zusätzlich die amtliche Struktur-Regel, die die meisten zufälligen 11-Ziffern-Folgen
    aussortiert: die erste Ziffer ist nicht 0, und in den ersten zehn Ziffern kommt
    **genau eine** Ziffer mehrfach vor (zwei- oder dreimal); bei dreimal nicht an
    unmittelbar aufeinanderfolgenden Stellen.
    """
    digits = _digits(value)
    if len(digits) != 11 or digits[0] == "0":
        return False

    head = digits[:10]
    counts = {d: head.count(d) for d in set(head)}
    repeated = [d for d, n in counts.items() if n > 1]
    if len(repeated) != 1:
        return False
    if counts[repeated[0]] > 3:
        return False
    if counts[repeated[0]] == 3 and re.search(rf"{repeated[0]}{{2}}", head):
        return False

    produkt = 10
    for ch in head:
        summe = (int(ch) + produkt) % 10
        if summe == 0:
            summe = 10
        produkt = (summe * 2) % 11
    return (11 - produkt) % 10 == int(digits[10])


def _cross_sum(n: int) -> int:
    return sum(int(c) for c in str(n))


def validate_svnr(value: str) -> bool:
    """Versicherungsnummer der DRV (Sozialversicherungsnummer), 12 Stellen.

    Aufbau: Bereichsnummer(2) · Geburtsdatum TTMMJJ(6) · Anfangsbuchstabe(1) ·
    Seriennummer(2) · Prüfziffer(1). Der Buchstabe wird zu zwei Ziffern (A=01…Z=26),
    die entstehenden 12 Ziffern mit 2,1,2,5,7,1,2,1,2,1,2,1 gewichtet; die Summe der
    Quersummen der Produkte mod 10 ist die Prüfziffer.
    """
    compact = re.sub(r"\s", "", value).upper()
    if not re.fullmatch(r"\d{8}[A-Z]\d{3}", compact):
        return False

    tag, monat = int(compact[2:4]), int(compact[4:6])
    if not (1 <= tag <= 31 and 1 <= monat <= 12):
        return False

    letter_ordinal = ord(compact[8]) - 64  # A=1 … Z=26
    expanded = compact[:8] + f"{letter_ordinal:02d}" + compact[9:11]
    weights = (2, 1, 2, 5, 7, 1, 2, 1, 2, 1, 2, 1)
    total = sum(_cross_sum(int(d) * w) for d, w in zip(expanded, weights, strict=True))
    return total % 10 == int(compact[11])


def validate_kvnr(value: str) -> bool:
    """Krankenversichertennummer (unveränderbarer Teil), 10 Stellen: Buchstabe + 9 Ziffern.

    Buchstabe zu zwei Ziffern (A=01…Z=26), zusammen mit den acht folgenden Ziffern
    alternierend 1,2 gewichtet; Summe der Quersummen der Produkte mod 10 = Prüfziffer.
    """
    compact = re.sub(r"\s", "", value).upper()
    if not re.fullmatch(r"[A-Z]\d{9}", compact):
        return False

    letter_ordinal = ord(compact[0]) - 64
    expanded = f"{letter_ordinal:02d}" + compact[1:9]
    weights = (1, 2, 1, 2, 1, 2, 1, 2, 1, 2)
    total = sum(_cross_sum(int(d) * w) for d, w in zip(expanded, weights, strict=True))
    return total % 10 == int(compact[9])


def validate_luhn(value: str) -> bool:
    """Kreditkartennummer nach Luhn (ISO/IEC 7812), 13–19 Ziffern."""
    digits = _digits(value)
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, ch in enumerate(reversed(digits)):
        digit = int(ch)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _valid_ip(token: str) -> bool:
    try:
        ipaddress.ip_address(token)
        return True
    except ValueError:
        return False


def validate_ipv6(value: str) -> bool:
    """Echte IPv6 — das Muster ist absichtlich grob, die Validierung zieht die Grenze."""
    return _valid_ip(value.strip())


# ---- Muster --------------------------------------------------------------------------

IBAN = DenyPattern(
    finding="IBAN erkannt (Mod-97 geprüft)",
    pattern=re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){10,30}\b"),
    per_match=True,
    validate=validate_iban,
    placeholder="[IBAN]",
)
STEUER_ID = DenyPattern(
    finding="Steuer-Identifikationsnummer erkannt (Prüfziffer geprüft)",
    pattern=re.compile(r"\b\d{2}[ ]?\d{3}[ ]?\d{3}[ ]?\d{3}\b|\b\d{11}\b"),
    per_match=True,
    validate=validate_steuer_id,
    placeholder="[STEUER_ID]",
)
SVNR = DenyPattern(
    finding="Sozialversicherungsnummer erkannt (Prüfziffer geprüft)",
    pattern=re.compile(r"\b\d{2}[ ]?\d{6}[ ]?[A-Z][ ]?\d{3}\b"),
    per_match=True,
    validate=validate_svnr,
    placeholder="[SVNR]",
)
KVNR = DenyPattern(
    finding="Krankenversichertennummer erkannt (Prüfziffer geprüft)",
    pattern=re.compile(r"\b[A-Z]\d{9}\b"),
    per_match=True,
    validate=validate_kvnr,
    placeholder="[KVNR]",
)
CREDIT_CARD = DenyPattern(
    finding="Kreditkartennummer erkannt (Luhn geprüft)",
    pattern=re.compile(r"\b\d{4}(?:[ -]?\d{4}){2,3}(?:[ -]?\d{1,3})?\b"),
    per_match=True,
    validate=validate_luhn,
    placeholder="[CREDITCARD]",
)
IPV6 = DenyPattern(
    finding="IPv6-Adresse erkannt",
    pattern=re.compile(r"\b(?:[0-9A-Fa-f]{0,4}:){2,7}(?:[0-9A-Fa-f]{0,4}|(?:\d{1,3}\.){3}\d{1,3})"),
    per_match=True,
    validate=validate_ipv6,
    placeholder="[IPV6]",
)
MAC = DenyPattern(
    finding="MAC-Adresse erkannt",
    pattern=re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"),
    placeholder="[MAC]",
)
KFZ = DenyPattern(
    finding="KFZ-Kennzeichen erkannt",
    # Unterscheidungszeichen(1–3) · Trenner · Erkennungsbuchstaben(1–2) · Zahl(1–4)
    # [+ E/H für Elektro-/Oldtimer]. Der Trenner ist Pflicht — ohne ihn wäre jede
    # Buchstaben-Ziffern-Folge ein Treffer.
    pattern=re.compile(r"\b[A-ZÄÖÜ]{1,3}[- ][A-Z]{1,2}[- ]?\d{1,4}[EH]?\b"),
    placeholder="[KFZ]",
)
PHONE_DE = DenyPattern(
    finding="Telefonnummer erkannt",
    # +49/0049/0 · Vorwahl (2–5, auch geklammert) · Teilnehmernummer, übliche Trenner.
    pattern=re.compile(
        r"(?:\+49|0049|\b0)[ /-]?(?:\(0?\d{2,5}\)|\d{2,5})[ /-]?\d{3,9}(?:[ /-]?\d{1,6})?\b"
    ),
    placeholder="[PHONE]",
)

PROFILE = DetectorProfile(
    name="pii_de",
    # Reihenfolge ist für die Span-Mechanik (§5.3) bedeutungslos — dort gewinnt der
    # längere Treffer, nicht der zuerst deklarierte. Für `verify_no_identifiers`
    # (Befundliste) bleibt sie nur die Reihenfolge der Meldungen.
    deny=(
        IBAN,
        STEUER_ID,
        SVNR,
        KVNR,
        CREDIT_CARD,
        EMAIL,
        IPV4,
        IPV6,
        MAC,
        KFZ,
        PHONE_DE,
        SECRET,
    ),
)
