"""Dispatch — Guard → Provider-Adapter → (reverse) (Spec §7.3, Revision 2).

Der EINZIGE Ort, der Provider-Adapter aufruft. Reihenfolge zwingend:
erst `guarded_egress` (Gate → Modus → Verifier → Audit, §2), erst bei
`released=true` geht der sanitisierte Text an den Adapter — nie davor.

Antwortpfad (§7.3/§8): bei pseudonymizing läuft die Provider-Antwort durch
`reverse_text` bzw. `stream_reverser` DERSELBEN Modus-Instanz, die der Guard
benutzt hat (Scope-Konsistenz über `select_mode`-Cache).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import structlog

from sluice.audit import AuditLog
from sluice.capacity import CapacityStore, StreamTelemetry, Usage
from sluice.capacity import capacity_store as _default_capacity
from sluice.dialect import messages_carry_tool_artifacts
from sluice.guard import guarded_egress
from sluice.policy import Profile
from sluice.providers import (
    TOOL_CAPABLE_PROVIDERS,
    ProviderAdapter,
    canonical_provider,
    select_egress_adapter,
)
from sluice.modes import EgressPayload, Mode, Scope, select_mode

log = structlog.get_logger("sluice.dispatch")

# Muss dem Default-Scope des PseudonymizingMode entsprechen (dort `_DEFAULT_SCOPE`):
# ohne expliziten Scope landen forward und reverse im selben Mapping.
_DEFAULT_SCOPE = Scope(key="global")


@dataclass
class CompletionOutcome:
    """Ergebnis eines geguardeten Provider-Aufrufs."""

    released: bool
    reason: str
    response_text: str | None = None
    provider: str | None = None
    model: str | None = None
    # Rev. 12: trägt die Blockade-*Art* aus dem Guard weiter (§5.3). None = Policy-/
    # Verifier-Entscheidung; `mode_unavailable` = ein Modus konnte nicht liefern.
    error_type: str | None = None
    # Rev. 14 (§7.2): vom Modell angeforderte Tool-Calls (neutral, `{id, name, arguments}`)
    # und der normalisierte Stop-Grund. None/`"stop"` = kein Tool-Call.
    tool_calls: list[dict[str, object]] | None = None
    finish_reason: str | None = None
    # Rev. 16 (§7.6): Token-Verbrauch dieses Aufrufs, sofern der Provider ihn meldet.
    # None = unbekannt, nicht null.
    usage: Usage | None = None


@dataclass
class StreamOutcome:
    """Wie CompletionOutcome, aber mit Antwort-Deltas statt fertigem Text."""

    released: bool
    reason: str
    chunks: AsyncIterator[str] | None = None
    error_type: str | None = None  # wie CompletionOutcome (Rev. 12, §5.3)


def _tool_capability_reason(payload: EgressPayload, provider_target: str) -> str | None:
    """Kann der Ziel-Adapter diesen (Tool-)Turn überhaupt abbilden? (§7.2)

    Greift für die deklarierten Specs **und** für einen laufenden Tool-Turn ohne neue
    Specs (`tool_calls`/`role:"tool"` in den Messages) — sonst ginge eine Konversation an
    einen Provider, der ihre Form nicht liest. Läuft **vor** dem Guard: ein Audit-Eintrag
    `released=true` für eine nie gesendete Anfrage wäre ein Prüfbericht über nichts.
    """
    if not (payload.tools or messages_carry_tool_artifacts(payload.messages)):
        return None
    if canonical_provider(provider_target) in TOOL_CAPABLE_PROVIDERS:
        return None
    return (
        f"Tool-Calling trägt derzeit nur {', '.join(TOOL_CAPABLE_PROVIDERS)}; "
        f"Provider '{provider_target}' (noch) nicht — fail-closed statt "
        f"stillem Weglassen (§7.2)."
    )


async def _guard(
    *,
    profile: Profile | None,
    purpose: str,
    payload: EgressPayload,
    scope: Scope | None,
    provider_target: str,
    provider_selection: str | None,
    mode: Mode | None,
    audit: AuditLog | None,
) -> tuple[list[dict[str, object]] | None, Mode | None, str, str | None]:
    """Gemeinsamer Guard-Vorlauf; gibt (Messages, Modus, reason, error_type) zurück."""
    outcome = await guarded_egress(
        profile=profile,
        purpose=purpose,
        payload=payload,
        scope=scope,
        provider_target=provider_target,
        provider_selection=provider_selection,
        mode=mode,
        audit=audit,
    )
    if not outcome.released:
        return None, None, outcome.reason, outcome.error_type

    assert profile is not None  # released=true impliziert ein Profil (Invariante 1)
    chosen = mode if mode is not None else select_mode(profile)
    messages = outcome.sanitized_messages
    if messages is None:
        assert outcome.sanitized_text is not None
        messages = [{"role": "user", "content": outcome.sanitized_text}]
    return messages, chosen, outcome.reason, outcome.error_type


async def guarded_completion(
    *,
    profile: Profile | None,
    purpose: str,
    payload: EgressPayload,
    provider_target: str,
    model: str,
    scope: Scope | None = None,
    max_tokens: int = 1024,
    mode: Mode | None = None,
    audit: AuditLog | None = None,
    adapter: ProviderAdapter | None = None,
    provider_selection: str | None = None,
    capacity: CapacityStore | None = None,
) -> CompletionOutcome:
    """Geguardete Completion: Gate → Modus → Verifier → Audit → Provider → reverse.

    adapter: Injektion für Tests; sonst Gateway-Adapter aus `provider_target`
    (§7.3, Rev. 7 — ohne konfiguriertes Gateway fail-closed, nie direkt).
    Blockt der Guard, wird der Adapter NIE berührt (fail-closed, Invariante 2).

    **Tools (Rev. 15, §7.2)** stehen in `payload.tools` — es gibt bewusst keinen
    Dispatch-Parameter daneben. Damit gibt es keinen Weg, Tools zu senden, ohne dass der
    Guard sie sieht; die Chokepoint-Eigenschaft ist strukturell und nicht per Konvention
    (§1). Der Guard verifiziert die Specs als `readonly`-Fläche (geprüft, nie
    umgeschrieben) — deshalb ist Tool-Calling seit Rev. 15 unter **jedem** Modus möglich,
    nicht nur unter `passthrough`. Hier bleibt nur die Adapter-Fähigkeit zu prüfen.

    **Rotation (Rev. 16, §4.5)** passiert NICHT hier: `provider_target`/`model` stehen
    schon fest, wenn der Dispatch beginnt. Er bekommt nur `provider_selection` — die
    Begründung fürs Audit — durchgereicht. So bleibt die Reihenfolge unverändert
    (Wahl → Gate → Modus → Verifier → Audit → Provider), und ein rotiertes Ziel wird
    von der Allowlist (§4.1) genauso geprüft wie ein vom Konsumenten genanntes.

    **Kein stilles Ausweichen:** scheitert der gewählte Provider, ist das ein Fehler mit
    Namen — kein Retry auf einem anderen. Sonst verstecke man genau die Information, die
    die Rotation sammeln soll: welches Modell unzuverlässig ist (§4.5/§10).
    """
    unsupported = _tool_capability_reason(payload, provider_target)
    if unsupported is not None:
        return CompletionOutcome(released=False, reason=unsupported)

    messages, chosen, reason, error_type = await _guard(
        profile=profile,
        purpose=purpose,
        payload=payload,
        scope=scope,
        provider_target=provider_target,
        provider_selection=provider_selection,
        mode=mode,
        audit=audit,
    )
    if messages is None or chosen is None:
        return CompletionOutcome(released=False, reason=reason, error_type=error_type)

    provider = adapter if adapter is not None else select_egress_adapter(provider_target)
    extra = {"tools": payload.tools} if payload.tools else {}
    response = await provider.complete(messages, model=model, max_tokens=max_tokens, **extra)

    # Kapazitäts-Telemetrie verbuchen (Rev. 16, §7.6) — mit dem Modell, das TATSÄCHLICH
    # geantwortet hat, nicht dem angefragten. Der Aufruf zählt auch dann, wenn nichts
    # gemeldet wurde: dass gerufen wurde, ist selbst eine Auskunft.
    (capacity if capacity is not None else _default_capacity).record(
        provider=response.provider or provider_target,
        model=response.model or model,
        usage=response.usage,
        rate_limit=response.rate_limit,
    )

    effective_scope = scope if scope is not None else _DEFAULT_SCOPE
    text = response.text
    if chosen.reversible:
        text = await chosen.reverse_text(text, effective_scope)

    tool_calls = (
        [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in response.tool_calls]
        if response.tool_calls
        else None
    )
    if tool_calls and chosen.reversible:
        # Tool-Arg-Reversal (§7.2/§8): das Modell hat auf Pseudonymen gearbeitet und gibt
        # sie in den Argumenten zurück. Der Konsument führt das Tool lokal auf **echten**
        # Werten aus — bekäme er hier Pseudonyme, während der Antworttext bereits
        # zurückgemappt ist, wäre die Antwort in sich widersprüchlich und das Tool liefe
        # auf einem Platzhalter. Dieselbe Modus-Instanz, derselbe Scope wie forward().
        for call in tool_calls:
            call["arguments"] = await chosen.reverse_obj(call["arguments"], effective_scope)

    log.info(
        "dispatch.completed",
        profile=profile.name if profile else None,
        provider=response.provider,
        model=response.model,
        tool_calls=len(response.tool_calls),
        # Token-Zahlen sind Metadaten und gehören ins strukturierte Log, nicht in den
        # egress_log-Eintrag (§6): der wird VOR dem Provider-Aufruf geschrieben und
        # trägt die Freigabe-Entscheidung, nicht deren Kosten.
        input_tokens=response.usage.input_tokens if response.usage else None,
        output_tokens=response.usage.output_tokens if response.usage else None,
        provider_selection=provider_selection,
    )
    return CompletionOutcome(
        released=True,
        reason=reason,
        response_text=text,
        provider=response.provider,
        model=response.model,
        tool_calls=tool_calls,
        finish_reason="tool_calls" if tool_calls else "stop",
        usage=response.usage,
    )


async def guarded_stream(
    *,
    profile: Profile | None,
    purpose: str,
    payload: EgressPayload,
    provider_target: str,
    model: str,
    scope: Scope | None = None,
    max_tokens: int = 1024,
    mode: Mode | None = None,
    audit: AuditLog | None = None,
    adapter: ProviderAdapter | None = None,
    provider_selection: str | None = None,
    capacity: CapacityStore | None = None,
) -> StreamOutcome:
    """Wie `guarded_completion`, aber streamend (§7.2).

    Bei pseudonymizing laufen die Deltas durch den Holdback-Puffer des
    `stream_reverser` — ein Pseudonym kann über zwei Chunks reichen
    (kritischer Failure-Mode Streaming-Passthrough).

    **Mit `payload.tools` wird fail-closed abgewiesen** (§7.2): der Stream-Pfad trägt
    keine `tool_calls`, und der Adapter nähme `tools` gar nicht erst entgegen — die Tools
    fielen still weg und der Konsument bekäme eine Antwort ohne die Aktionen, die er
    angeboten hat. Der Tool-Loop läuft ohnehin nicht-streamend (§6.5).
    """
    if payload.tools:
        return StreamOutcome(
            released=False,
            reason=(
                "Streaming mit tools wird nicht unterstützt; den Tool-Loop "
                "nicht-streamend fahren (stream=false) — fail-closed statt "
                "stillem Weglassen der Tools (§7.2)."
            ),
        )

    # Ein Verlauf MIT vergangenen Tool-Turns darf streamen (nur neue `tools` nicht) —
    # der Ziel-Adapter muss die Form aber lesen können.
    unsupported = _tool_capability_reason(payload, provider_target)
    if unsupported is not None:
        return StreamOutcome(released=False, reason=unsupported)

    messages, chosen, reason, error_type = await _guard(
        profile=profile,
        purpose=purpose,
        payload=payload,
        scope=scope,
        provider_target=provider_target,
        provider_selection=provider_selection,
        mode=mode,
        audit=audit,
    )
    if messages is None or chosen is None:
        return StreamOutcome(released=False, reason=reason, error_type=error_type)

    provider = adapter if adapter is not None else select_egress_adapter(provider_target)
    reverser = (
        chosen.stream_reverser(scope if scope is not None else _DEFAULT_SCOPE)
        if chosen.reversible
        else None
    )

    telemetry = StreamTelemetry()
    store = capacity if capacity is not None else _default_capacity

    async def _chunks() -> AsyncIterator[str]:
        async for delta in provider.stream(
            messages, model=model, max_tokens=max_tokens, telemetry=telemetry
        ):
            out = reverser.feed(delta) if reverser is not None else delta
            if out:
                yield out
        if reverser is not None:
            tail = reverser.flush()
            if tail:
                yield tail
        # Erst NACH dem letzten Delta verbuchen (Rev. 16, §7.6): vorher steht der
        # Verbrauch eines Streams nicht fest. Bricht der Stream ab, wird nichts
        # verbucht — ein halber Wert wäre im Snapshot von einem ganzen nicht zu
        # unterscheiden.
        store.record(
            provider=provider_target,
            model=model,
            usage=telemetry.usage,
            rate_limit=telemetry.rate_limit,
        )

    return StreamOutcome(released=True, reason=reason, chunks=_chunks())
