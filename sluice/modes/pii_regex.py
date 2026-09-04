"""PiiRegexMode — die Regex-Stufe allein (Spec §3/§5.3, Rev. 12).

Erkennt strukturierte Identifikatoren über die Muster des Detektor-Profils und ersetzt
sie span-basiert durch typisierte Platzhalter. Wo eine Prüfsumme existiert (IBAN Mod-97,
Steuer-ID, SVNR, KVNR, Luhn — siehe `detectors/pii_de.py`), erreicht diese Stufe volle
Präzision: ein bestandenes Prüfziffernverfahren *ist* die Bestätigung des Typs, da rät
nichts. Genau deshalb hebt `pii_ner` sie nicht auf, sondern setzt darauf auf (§5.3).

Unterschied zu `strict` (§3): `strict` redigiert über sequentielle `re.sub`-Ketten
(`verifier.redact_identifiers`) — bewährt und unverändert. Diese Stufe arbeitet
**span-basiert** (`sluice/spans.py`), weil nur eine gemeinsame Offset-Koordinate die
Vereinigung mit einer zweiten Erkennungsstufe erlaubt. Nebeneffekt: überlappende Muster
werden nach *Länge* aufgelöst statt nach Deklarationsreihenfolge, ein Treffer kann also
nicht mehr von einem früheren Muster zerschnitten werden.

`reversible=False` (kein Mapping, zustandslos), `enforce_verifier=True` — der
deterministische Riegel (§5) bleibt der harte Boden darunter, fail-closed.
"""

from __future__ import annotations

from typing import Any

from sluice.content import message_surfaces, rebuild_message
from sluice.identity import AnonymizationIdentity, build_identity
from sluice.modes import EgressPayload, Sanitized, Scope, StreamReverserProtocol
from sluice.spans import Span, apply_spans, detect_regex_spans, merge_spans


class PiiRegexMode:
    reversible = False
    name = "pii_regex"
    enforce_verifier = True  # der Verifier bleibt der Boden unter der Redaktion (§5)

    def __init__(
        self,
        *,
        detector_profile: str = "pii_de",
        dictionary_terms: tuple[str, ...] = (),
    ) -> None:
        self._detector_profile = detector_profile
        self._dictionary_terms = dictionary_terms

    def identity(self) -> AnonymizationIdentity:
        """Anonymisierungs-Identität dieses Modus (§5.4) — ohne NER-Anteil."""
        return build_identity(
            mode=self.name,
            detector_profile=self._detector_profile,
            dictionary_terms=self._dictionary_terms,
        )

    async def spans_for(self, text: str) -> tuple[Span, ...]:
        """Die erkannten Spans — der Einstiegspunkt, den `pii_ner` erweitert (§5.3)."""
        return merge_spans(
            detect_regex_spans(
                text, self._detector_profile, dictionary_terms=self._dictionary_terms
            )
        )

    async def forward(self, payload: EgressPayload, scope: Scope | None) -> Sanitized:
        """Redigiert die Proxy-Form (messages) je Text-Inhalt, sonst den Rohtext.

        Fail-closed wie `strict`: ohne Inhalt gibt es keinen Kandidaten, und der Guard
        blockiert (kein stiller Durchlass). Was die Muster übersehen, fängt der Verifier
        danach ab.
        """
        if payload.messages is not None:
            redacted = [await self._redact_message(m) for m in payload.messages]
            return Sanitized(messages=redacted)
        return Sanitized(text=await self._redact(payload.raw_text))

    async def _redact(self, text: str) -> str:
        return apply_spans(text, await self.spans_for(text))

    async def _redact_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Redigiert **jede** Textfläche der Message, nicht nur `content:str` (§5.5).

        Jede Fläche wird einzeln erkannt und redigiert — Spans sind Offsets *in ihrem
        Text*, eine Fläche darf also nie mit einer anderen zusammengeklebt werden.
        Was sich nicht aufzählen lässt, blockiert im Guard fail-closed.
        """
        surfaces = message_surfaces(message)
        if not surfaces.texts:
            return message
        redacted = [await self._redact(t) for t in surfaces.texts]
        return rebuild_message(message, redacted)

    async def reverse_text(self, text: str, scope: Scope) -> str:
        raise NotImplementedError("PiiRegexMode ist irreversibel (§3).")

    def stream_reverser(self, scope: Scope) -> StreamReverserProtocol:
        raise NotImplementedError("PiiRegexMode ist irreversibel (§3).")

    async def reverse_obj(self, obj: dict[str, Any], scope: Scope) -> dict[str, Any]:
        raise NotImplementedError("PiiRegexMode ist irreversibel (§3).")
