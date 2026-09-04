"""StrictMode — auto-redigierend, Auslieferungs-Default (Spec §3/§4.3, Rev. 9).

Der sichere Default-Modus: `forward()` fährt selbst die Detektor-Engine (§5.1) über den
*Rohtext*, ersetzt jeden Treffer durch einen typisierten Platzhalter (`[IP]`, `[EMAIL]`,
`[SECRET]`, …) und überlässt danach dem Guard den deterministischen Verifier als Boden
(`enforce_verifier=True`) — bleibt ein roher Identifier stehen, wird **blockiert**
(fail-closed, kein Rest-Leck).

Anders als `generalizing` verlangt `strict` **keinen** vom Konsumenten vor-generalisierten
Text; er sanitisiert eigenständig über die im Profil deklarierten Muster (`detector_profile`,
§4). `reversible=False`: irreversibel, kein Mapping, zustandslos.

Die Redaktion ist reine Mechanik über die *deklarierten* Muster — die semantische
Generalisierung bleibt Konsumenten-Domäne (§1.1) und findet hier NICHT statt.
"""

from __future__ import annotations

from typing import Any

from sluice.content import message_surfaces, rebuild_message
from sluice.modes import EgressPayload, Sanitized, Scope, StreamReverserProtocol
from sluice.verifier import redact_identifiers


class StrictMode:
    reversible = False
    name = "strict"
    enforce_verifier = True  # der Verifier ist der harte Boden unter der Redaktion (§5)

    def __init__(
        self, *, detector_profile: str = "infra", dictionary_terms: tuple[str, ...] = ()
    ) -> None:
        self._detector_profile = detector_profile
        self._dictionary_terms = dictionary_terms

    async def forward(self, payload: EgressPayload, scope: Scope | None) -> Sanitized:
        """Redigiert eigenständig: Proxy-Form (messages) je Text-Inhalt, sonst den Rohtext.

        Fail-closed: ohne Inhalt gibt es keinen Kandidaten — der Guard blockiert (kein
        stiller Durchlass). Was die Muster übersehen, fängt der Verifier danach ab.
        """
        if payload.messages is not None:
            redacted = [self._redact_message(m) for m in payload.messages]
            return Sanitized(messages=redacted)
        return Sanitized(text=self._redact(payload.raw_text))

    def _redact(self, text: str) -> str:
        return redact_identifiers(
            text, self._detector_profile, dictionary_terms=self._dictionary_terms
        )

    def _redact_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Redigiert **jede** Textfläche der Message, nicht nur `content:str` (§5.5).

        Block-Listen, Tool-Result-Inhalte und Tool-Argumente gehören dazu — sonst ginge
        genau dort ungeprüfter Inhalt raus (`sluice/content.py`). Flächen, die sich nicht
        aufzählen lassen, bleiben hier unangetastet und blockieren im Guard fail-closed.
        """
        surfaces = message_surfaces(message)
        if not surfaces.texts:
            return message
        return rebuild_message(message, [self._redact(t) for t in surfaces.texts])

    async def reverse_text(self, text: str, scope: Scope) -> str:
        raise NotImplementedError("StrictMode ist irreversibel (Auto-Redaktion, §3).")

    def stream_reverser(self, scope: Scope) -> StreamReverserProtocol:
        raise NotImplementedError("StrictMode ist irreversibel (Auto-Redaktion, §3).")

    async def reverse_obj(self, obj: dict[str, Any], scope: Scope) -> dict[str, Any]:
        raise NotImplementedError("StrictMode ist irreversibel (Auto-Redaktion, §3).")
