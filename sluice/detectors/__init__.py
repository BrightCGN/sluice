"""Detektor-Profile — Muster pro Domäne, Engine geteilt (Spec §5.1).

Die Verifier-*Engine* (sluice/verifier.py) ist generischer Mechanismus; *welche*
Identifier für eine Domäne zählen, deklariert der Konsument über sein Profil
(`detector_profile`, Spec §4). Diese Trennung ist die Grenze Mechanismus/Domäne
(Spec §1.1): Sluice liefert die Muster-Sets als wählbare Profile, zieht aber keine
Domänenlogik in den Kern.

Konservative Deny-Muster: lieber false-positive (blockt zu viel) als ein Leck.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DenyPattern:
    """Ein Deny-Muster der Verifier-Engine (Spec §5).

    finding:   Text des Befunds im Blockier-Grund (und Audit, Spec §6).
    per_match: True → jeder Treffer wird einzeln als Befund gelistet (z. B. IPs);
               False → ein Sammel-Befund, sobald das Muster irgendwo greift.
    validate:  optionaler Zusatz-Check pro Treffer (z. B. echte IPv4-Validierung).
    """

    finding: str
    pattern: re.Pattern[str]
    per_match: bool = False
    validate: Callable[[str], bool] | None = None
    placeholder: str = "[redacted]"  # Ersatz beim Auto-Redigieren (strict-Modus, §3)


@dataclass(frozen=True)
class DetectorProfile:
    """Ein benanntes Muster-Set, das der Verifier lädt (Spec §5.1)."""

    name: str
    deny: tuple[DenyPattern, ...] = field(default_factory=tuple)


from sluice.detectors.code import PROFILE as _CODE  # noqa: E402
from sluice.detectors.financial import PROFILE as _FINANCIAL  # noqa: E402
from sluice.detectors.infra import PROFILE as _INFRA  # noqa: E402
from sluice.detectors.media import PROFILE as _MEDIA  # noqa: E402
from sluice.detectors.pii_de import PROFILE as _PII_DE  # noqa: E402

_PROFILES: dict[str, DetectorProfile] = {
    _INFRA.name: _INFRA,
    _CODE.name: _CODE,
    _MEDIA.name: _MEDIA,
    _FINANCIAL.name: _FINANCIAL,
    _PII_DE.name: _PII_DE,  # deutsche PII mit Prüfsummen (Rev. 12, §5.3)
}


def get_detector_profile(name: str) -> DetectorProfile | None:
    """Muster-Set per Name; None bei unbekanntem Profil (Verifier blockt dann fail-closed)."""
    return _PROFILES.get(name)


# Trennzeichen des kanonischen Namens zusammengelegter Profile: `media+pii_de`.
COMPOSITE_SEPARATOR = "+"


class UnknownDetectorProfile(ValueError):
    """Ein Profil nennt ein Muster-Set, das es nicht gibt (§5.1, Rev. 13).

    Bewusst ein *Ladefehler*: bis Rev. 12 blieb ein Tippfehler im `detector_profile`
    still — die Profile luden, und erst der Verifier blockte später mit „unbekanntes
    Detektor-Profil". Das ist zwar fail-closed, aber als Fehlerbild unbrauchbar: der
    Betrieb sucht dann nach einem Defekt, wo eine Zeichendreher-Konfiguration liegt.
    """


def merge_detector_profiles(names: Sequence[str]) -> str:
    """Legt mehrere Muster-Sets zu einem zusammen und gibt dessen kanonischen Namen zurück.

    Der Grund für diese Funktion (§5.1, Rev. 13): Ein Profil braucht regelmäßig **beides**
    — die Muster seiner Domäne *und* die deutschen PII-Muster. `media` allein kennt keine
    IBAN, `pii_de` allein keine NAS-Pfade. Wer sich für eines entscheiden muss, tauscht
    Schutz in der eigenen Domäne gegen Schutz in einer fremden.

    Das Ergebnis wird **unter seinem kanonischen Namen in der Registry hinterlegt**. Damit
    bleibt alles Nachgelagerte unverändert: Verifier, Span-Erkennung, Modi und die
    Anonymisierungs-Identität arbeiten weiter mit *einem* Namen, und im Audit steht
    `media+pii_de` statt einer Liste.

    **Sortiert**, nicht in Deklarationsreihenfolge: die Vereinigung ist ordnungsunabhängig,
    also darf eine bloße Umordnung im Profil den Identitäts-Digest nicht bewegen — dieselbe
    Überlegung wie bei den Wörterbuch-Termen (§5.4).

    Ein einzelner Name bleibt unverändert und ergibt denselben Digest wie vor Rev. 13.
    """
    if not names:
        raise UnknownDetectorProfile("Leere Detektor-Profil-Liste (§5.1, fail-closed).")

    eindeutig = sorted({n.strip() for n in names if n and n.strip()})
    if not eindeutig:
        raise UnknownDetectorProfile("Leere Detektor-Profil-Liste (§5.1, fail-closed).")

    unbekannt = [n for n in eindeutig if n not in _PROFILES]
    if unbekannt:
        raise UnknownDetectorProfile(
            f"Unbekannte Detektor-Profile: {', '.join(unbekannt)}. "
            f"Verfügbar: {', '.join(sorted(_PROFILES))}."
        )

    if len(eindeutig) == 1:
        return eindeutig[0]

    kanonisch = COMPOSITE_SEPARATOR.join(eindeutig)
    if kanonisch in _PROFILES:
        return kanonisch

    # Dubletten entfernen: `media` und `pii_de` führen beide E-Mail und IP. Doppelte
    # Muster wären zwar harmlos (identische Spans, die `merge_spans` ohnehin auflöst),
    # aber sie kosten Laufzeit und blähen die Befundliste im Audit auf.
    gesehen: set[tuple[str, str, str, bool]] = set()
    zusammen: list[DenyPattern] = []
    for name in eindeutig:
        for deny in _PROFILES[name].deny:
            schluessel = (deny.pattern.pattern, deny.placeholder, deny.finding, deny.per_match)
            if schluessel in gesehen:
                continue
            gesehen.add(schluessel)
            zusammen.append(deny)

    _PROFILES[kanonisch] = DetectorProfile(name=kanonisch, deny=tuple(zusammen))
    return kanonisch


# Redaktions-Platzhalter für Wörterbuch-Treffer (Rev. 10). Der Hauptfall ist der
# personenbezogene Name; nicht-Namen werden ebenso sicher ersetzt, nur so etikettiert.
DICTIONARY_PLACEHOLDER = "[NAME]"


def build_dictionary_patterns(terms: Iterable[str]) -> tuple[DenyPattern, ...]:
    """Baut Deny-Muster aus konsument-deklarierten Wörterbuch-Termen (Spec §5.1, Rev. 10).

    Jeder Term wird **literal** (regex-escaped), **wortgrenzen-gebunden** und
    **case-insensitiv** gematcht — das deckt freie Namen/Adressen ab, die die generischen
    Regex-Muster (`infra`/`media`) prinzipbedingt nicht erkennen. Leere/whitespace-Terme
    werden übersprungen. Die *Liste* ist Domäne (Profil), das *Matching* ist Mechanismus.
    """
    patterns: list[DenyPattern] = []
    for term in terms:
        cleaned = term.strip()
        if not cleaned:
            continue
        patterns.append(
            DenyPattern(
                finding="Wörterbuch-Term erkannt",
                pattern=re.compile(rf"\b{re.escape(cleaned)}\b", re.IGNORECASE),
                placeholder=DICTIONARY_PLACEHOLDER,
            )
        )
    return tuple(patterns)
