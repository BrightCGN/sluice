"""GeneralizingStrategy — Einbahnstraße, DEFAULT (Spec §3).

Herkunft: Tempers egress/-Datenfluss (guard.py ruft nur verify_no_identifiers).
`reversible=False`: irreversibel → aus DSGVO-Scope, zustandslos.

Die *semantische* Generalisierung (aus einem validierten Fix die übertragbare Lektion
machen) ist Konsumenten-Domäne (§1.1) und passiert NICHT hier — `forward()` reicht nur
den vom Konsumenten gelieferten, bereits generalisierten Text als Egress-Kandidaten
weiter; ob er egress-tauglich ist, entscheidet der Verifier im Guard (Invariante 2).
"""

from __future__ import annotations

from typing import Any

from sluice.strategies import EgressPayload, Sanitized, Scope, StreamReverserProtocol


class GeneralizingStrategy:
    reversible = False
    name = "generalizing"

    async def forward(self, payload: EgressPayload, scope: Scope | None) -> Sanitized:
        """Reicht den generalisierten Konsumenten-Text als Egress-Kandidaten durch.

        Fail-closed: ohne generalisierten Text gibt es keinen Kandidaten — Fehler,
        nicht stiller Durchlass des Rohtexts.
        """
        if payload.generalized_text is None:
            raise ValueError(
                "GeneralizingStrategy braucht payload.generalized_text — die semantische "
                "Generalisierung macht der Konsument (§1.1), Sluice verifiziert nur."
            )
        return Sanitized(text=payload.generalized_text)

    async def reverse_text(self, text: str, scope: Scope) -> str:
        raise NotImplementedError("GeneralizingStrategy ist irreversibel (Einbahnstraße, §3).")

    def stream_reverser(self, scope: Scope) -> StreamReverserProtocol:
        raise NotImplementedError("GeneralizingStrategy ist irreversibel (Einbahnstraße, §3).")

    async def reverse_obj(self, obj: dict[str, Any], scope: Scope) -> dict[str, Any]:
        raise NotImplementedError("GeneralizingStrategy ist irreversibel (Einbahnstraße, §3).")
