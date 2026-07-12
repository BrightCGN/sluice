"""PassthroughStrategy — der triviale Modus (Spec §2.1, Rev. 9).

Kein Bypass an der Boundary vorbei: der Payload läuft durch dieselbe Kette
(Profil-Gate → Modus → Audit), aber dieser Modus **transformiert nicht und komponiert
den Verifier nicht** (`enforce_verifier=False`) — er reicht den Text unverändert an den
Dispatch. Anwendungsfälle: bereits nicht-sensible Daten, vertrauenswürdige Ziele und
ausdrücklich Experimentieren (§2.1).

**Explizites Opt-in.** Nie der Auslieferungs-Default (`strict`, §4.3), nie geerbt — nur
wenn Profil/Request `passthrough` ausdrücklich wählt und `allowed_modes` es zulässt
(§4.1). Das Profil-Gate (§4.2) läuft trotzdem zuerst: `egress_enabled=false` blockt auch
`passthrough`. Wer ihn wählt, trägt das Egress-Risiko selbst (§2.1).

`reversible=False`: kein Mapping, kein Rückweg — der Text geht so raus, wie er reinkam.
"""

from __future__ import annotations

from typing import Any

from sluice.strategies import EgressPayload, Sanitized, Scope, StreamReverserProtocol


class PassthroughStrategy:
    reversible = False
    name = "passthrough"
    enforce_verifier = False  # bewusst KEIN Verifier — die Konsumenten-Entscheidung (§2.1)

    async def forward(self, payload: EgressPayload, scope: Scope | None) -> Sanitized:
        """Reicht den Payload unverändert durch — Proxy-Form (messages) bevorzugt,
        sonst der Rohtext. Ohne Inhalt gibt es keinen Kandidaten (fail-closed im Guard)."""
        if payload.messages is not None:
            return Sanitized(messages=payload.messages)
        return Sanitized(text=payload.raw_text)

    async def reverse_text(self, text: str, scope: Scope) -> str:
        raise NotImplementedError("PassthroughStrategy ist irreversibel (kein Mapping, §2.1).")

    def stream_reverser(self, scope: Scope) -> StreamReverserProtocol:
        raise NotImplementedError("PassthroughStrategy ist irreversibel (kein Mapping, §2.1).")

    async def reverse_obj(self, obj: dict[str, Any], scope: Scope) -> dict[str, Any]:
        raise NotImplementedError("PassthroughStrategy ist irreversibel (kein Mapping, §2.1).")
