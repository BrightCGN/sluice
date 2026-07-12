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
from sluice.guard import guarded_egress
from sluice.policy import Profile
from sluice.providers import ProviderAdapter, select_egress_adapter
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


@dataclass
class StreamOutcome:
    """Wie CompletionOutcome, aber mit Antwort-Deltas statt fertigem Text."""

    released: bool
    reason: str
    chunks: AsyncIterator[str] | None = None


async def _guard(
    *,
    profile: Profile | None,
    purpose: str,
    payload: EgressPayload,
    scope: Scope | None,
    provider_target: str,
    mode: Mode | None,
    audit: AuditLog | None,
) -> tuple[list[dict[str, object]] | None, Mode | None, str]:
    """Gemeinsamer Guard-Vorlauf; gibt (sanitisierte Messages, Modus, reason) zurück."""
    outcome = await guarded_egress(
        profile=profile,
        purpose=purpose,
        payload=payload,
        scope=scope,
        provider_target=provider_target,
        mode=mode,
        audit=audit,
    )
    if not outcome.released:
        return None, None, outcome.reason

    assert profile is not None  # released=true impliziert ein Profil (Invariante 1)
    chosen = mode if mode is not None else select_mode(profile)
    messages = outcome.sanitized_messages
    if messages is None:
        assert outcome.sanitized_text is not None
        messages = [{"role": "user", "content": outcome.sanitized_text}]
    return messages, chosen, outcome.reason


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
) -> CompletionOutcome:
    """Geguardete Completion: Gate → Modus → Verifier → Audit → Provider → reverse.

    adapter: Injektion für Tests; sonst Gateway-Adapter aus `provider_target`
    (§7.3, Rev. 7 — ohne konfiguriertes Gateway fail-closed, nie direkt).
    Blockt der Guard, wird der Adapter NIE berührt (fail-closed, Invariante 2).
    """
    messages, chosen, reason = await _guard(
        profile=profile,
        purpose=purpose,
        payload=payload,
        scope=scope,
        provider_target=provider_target,
        mode=mode,
        audit=audit,
    )
    if messages is None or chosen is None:
        return CompletionOutcome(released=False, reason=reason)

    provider = adapter if adapter is not None else select_egress_adapter(provider_target)
    response = await provider.complete(messages, model=model, max_tokens=max_tokens)

    text = response.text
    if chosen.reversible:
        text = await chosen.reverse_text(text, scope if scope is not None else _DEFAULT_SCOPE)

    log.info(
        "dispatch.completed",
        profile=profile.name if profile else None,
        provider=response.provider,
        model=response.model,
    )
    return CompletionOutcome(
        released=True,
        reason=reason,
        response_text=text,
        provider=response.provider,
        model=response.model,
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
) -> StreamOutcome:
    """Wie `guarded_completion`, aber streamend (§7.2).

    Bei pseudonymizing laufen die Deltas durch den Holdback-Puffer des
    `stream_reverser` — ein Pseudonym kann über zwei Chunks reichen
    (kritischer Failure-Mode Streaming-Passthrough).
    """
    messages, chosen, reason = await _guard(
        profile=profile,
        purpose=purpose,
        payload=payload,
        scope=scope,
        provider_target=provider_target,
        mode=mode,
        audit=audit,
    )
    if messages is None or chosen is None:
        return StreamOutcome(released=False, reason=reason)

    provider = adapter if adapter is not None else select_egress_adapter(provider_target)
    reverser = (
        chosen.stream_reverser(scope if scope is not None else _DEFAULT_SCOPE)
        if chosen.reversible
        else None
    )

    async def _chunks() -> AsyncIterator[str]:
        async for delta in provider.stream(messages, model=model, max_tokens=max_tokens):
            out = reverser.feed(delta) if reverser is not None else delta
            if out:
                yield out
        if reverser is not None:
            tail = reverser.flush()
            if tail:
                yield tail

    return StreamOutcome(released=True, reason=reason, chunks=_chunks())
