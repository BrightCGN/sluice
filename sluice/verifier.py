"""Deterministischer Verifier — der harte Riegel unter dem Modus (Spec §5).

Sicherheitsorientiert, **deterministisch**: garantiert, dass kein Identifier die
Grenze überquert. **Nicht** dem Modellurteil überlassen — ein LLM ist probabilistisch,
übersieht gelegentlich oder schleust beim Umformulieren wieder Konkretes ein.

Läuft in **beiden** Modi gleich streng (Spec §2, Invariante 2): „reversibel" ist kein
Grund, den Riegel zu lockern — auch dann darf nur ein *Pseudonym* raus, nie ein echter
Wert, den das Mapping übersah. Fail-closed: findet er *irgendeinen* rohen Identifier →
blockieren (clean=False). Lieber false-positive als ein Leck.

Engine aus Tempers egress/verifier.py, im Kern unverändert; die Muster kommen aus
dem Detektor-Profil des Konsumenten (Spec §5.1) statt hart verdrahtet.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from collections.abc import Sequence

from sluice.detectors import (
    DetectorProfile,
    build_dictionary_patterns,
    get_detector_profile,
)


@dataclass
class VerificationResult:
    """Ergebnis der deterministischen Prüfung."""

    clean: bool
    findings: list[str] = field(default_factory=list)


def verify_no_identifiers(
    text: str,
    detector_profile: str | DetectorProfile = "infra",
    *,
    dictionary_terms: Sequence[str] = (),
) -> VerificationResult:
    """Harter Riegel: findet *irgendeinen* Identifier → blockiert (clean=False).

    detector_profile: Name eines registrierten Muster-Sets (Spec §5.1) oder ein
    DetectorProfile-Objekt. Unbekannter Name → fail-closed blockieren, nicht raten.
    dictionary_terms: konsument-deklarierte Wörterbuch-Terme (Spec §5.1, Rev. 10) —
    literal/wortgrenzen/case-insensitiv geprüft, ergänzen die Muster des Profils.
    """
    if isinstance(detector_profile, str):
        profile = get_detector_profile(detector_profile)
        if profile is None:
            return VerificationResult(
                clean=False,
                findings=[f"unbekanntes Detektor-Profil '{detector_profile}' (fail-closed)"],
            )
    else:
        profile = detector_profile

    deny_patterns = (*profile.deny, *build_dictionary_patterns(dictionary_terms))

    findings: list[str] = []
    for deny in deny_patterns:
        if deny.per_match:
            for match in deny.pattern.findall(text):
                if deny.validate is None or deny.validate(match):
                    findings.append(f"{deny.finding}: {match}")
        elif deny.pattern.search(text):
            findings.append(deny.finding)

    return VerificationResult(clean=not findings, findings=findings)


def redact_identifiers(
    text: str,
    detector_profile: str | DetectorProfile = "infra",
    *,
    dictionary_terms: Sequence[str] = (),
) -> str:
    """Auto-Redaktion für den `strict`-Modus (Spec §3): ersetzt jeden Muster-Treffer des
    Detektor-Profils durch seinen typisierten Platzhalter (`[IP]`, `[EMAIL]`, …).

    Reine Mechanik über die *deklarierten* Muster (§1.1) — keine Domänen-Semantik. Bei
    unbekanntem Profil wird der Text **unverändert** zurückgegeben; der Verifier greift
    dann im Guard fail-closed (kein stiller Durchlass). Danach fährt der Guard
    `verify_no_identifiers` als Boden — bleibt ein roher Identifier stehen, wird blockiert.
    """
    if isinstance(detector_profile, str):
        profile = get_detector_profile(detector_profile)
        if profile is None:
            return text  # unbekannt → nicht redigieren; Guard-Verifier blockt fail-closed
    else:
        profile = detector_profile

    for deny in (*profile.deny, *build_dictionary_patterns(dictionary_terms)):
        if deny.validate is not None:
            text = deny.pattern.sub(
                lambda m, d=deny: d.placeholder if d.validate(m.group(0)) else m.group(0),
                text,
            )
        else:
            text = deny.pattern.sub(deny.placeholder, text)
    return text
