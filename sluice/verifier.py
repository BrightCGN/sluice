"""Deterministischer Verifier — der harte Riegel unter der Strategie (Spec §5).

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

from sluice.detectors import DetectorProfile, get_detector_profile


@dataclass
class VerificationResult:
    """Ergebnis der deterministischen Prüfung."""

    clean: bool
    findings: list[str] = field(default_factory=list)


def verify_no_identifiers(text: str, detector_profile: str | DetectorProfile = "infra") -> VerificationResult:
    """Harter Riegel: findet *irgendeinen* Identifier → blockiert (clean=False).

    detector_profile: Name eines registrierten Muster-Sets (Spec §5.1) oder ein
    DetectorProfile-Objekt. Unbekannter Name → fail-closed blockieren, nicht raten.
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

    findings: list[str] = []
    for deny in profile.deny:
        if deny.per_match:
            for match in deny.pattern.findall(text):
                if deny.validate is None or deny.validate(match):
                    findings.append(f"{deny.finding}: {match}")
        elif deny.pattern.search(text):
            findings.append(deny.finding)

    return VerificationResult(clean=not findings, findings=findings)
