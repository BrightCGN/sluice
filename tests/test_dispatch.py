"""Dispatch-Tests (Spec §7.3) — der Adapter wird NIE vor `released=true` berührt."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sluice.audit import AuditLog
from sluice.dispatch import guarded_completion, guarded_stream
from sluice.policy import Profile, ReversibleConfig
from sluice.providers import ProviderResponse
from sluice.strategies import EgressPayload, Scope
from sluice.strategies.pseudonymizing import PseudonymizingStrategy

TEMPER = Profile(
    name="temper",
    strategy="generalizing",
    egress_enabled=True,
    allowed_purposes=("external_escalation",),
    provider_allowlist=("claude", "gemini"),
    detector_profile="infra",
)

AIDER = Profile(
    name="aider-code",
    strategy="pseudonymizing",
    egress_enabled=True,
    allowed_purposes=("code_completion",),
    provider_allowlist=("claude",),
    detector_profile="infra",
    reversible=ReversibleConfig(),
)


class FakeAdapter:
    """Zeichnet auf, was ihn erreicht — und schlägt Alarm, wenn er blockiert erreicht wird."""

    name = "fake"

    def __init__(self, *, reply: str = "ok", stream_chunks: tuple[str, ...] = ()) -> None:
        self.reply = reply
        self.stream_chunks = stream_chunks
        self.calls: list[list[dict[str, Any]]] = []

    async def complete(
        self, messages: list[dict[str, Any]], *, model: str, max_tokens: int = 1024
    ) -> ProviderResponse:
        self.calls.append(messages)
        return ProviderResponse(text=self.reply, model=model, provider=self.name)

    async def stream(
        self, messages: list[dict[str, Any]], *, model: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        self.calls.append(messages)
        for chunk in self.stream_chunks:
            yield chunk


# ---------- Blockiert ⇒ Adapter unberührt (Invariante 2, fail-closed) ----------


async def test_blocked_guard_never_touches_provider() -> None:
    adapter = FakeAdapter()
    outcome = await guarded_completion(
        profile=None,  # Default-Deny (§4.3)
        purpose="external_escalation",
        payload=EgressPayload(raw_text="x", generalized_text="sauber"),
        provider_target="claude",
        model="claude-sonnet-5",
        adapter=adapter,
        audit=AuditLog(),
    )
    assert outcome.released is False
    assert outcome.response_text is None
    assert adapter.calls == []


async def test_provider_not_in_allowlist_never_touches_provider() -> None:
    adapter = FakeAdapter()
    outcome = await guarded_completion(
        profile=TEMPER,
        purpose="external_escalation",
        payload=EgressPayload(raw_text="x", generalized_text="sauber"),
        provider_target="openai",  # nicht in TEMPERs Allowlist (§4.1)
        model="gpt-x",
        adapter=adapter,
        audit=AuditLog(),
    )
    assert outcome.released is False
    assert adapter.calls == []


async def test_verifier_block_never_touches_provider() -> None:
    adapter = FakeAdapter()
    outcome = await guarded_completion(
        profile=TEMPER,
        purpose="external_escalation",
        payload=EgressPayload(raw_text="Host 10.0.0.5", generalized_text="Host 10.0.0.5"),
        provider_target="claude",
        model="claude-sonnet-5",
        adapter=adapter,
        audit=AuditLog(),
    )
    assert outcome.released is False
    assert "Verifier blockiert" in outcome.reason
    assert adapter.calls == []


# ---------- Released ⇒ Provider sieht NUR sanitisierten Text ----------


async def test_released_generalizing_sends_sanitized_text() -> None:
    adapter = FakeAdapter(reply="Antwort")
    outcome = await guarded_completion(
        profile=TEMPER,
        purpose="external_escalation",
        payload=EgressPayload(raw_text="Server db-prod-3 down", generalized_text="Ein Server ist down"),
        provider_target="claude",
        model="claude-sonnet-5",
        adapter=adapter,
        audit=AuditLog(),
    )
    assert outcome.released is True
    assert outcome.response_text == "Antwort"
    assert outcome.provider == "fake"
    assert adapter.calls == [[{"role": "user", "content": "Ein Server ist down"}]]


# ---------- Pseudonymizing: forward raus, reverse rein (§7.3/§8) ----------


async def test_pseudonymizing_roundtrip_reverses_response() -> None:
    strategy = PseudonymizingStrategy(dictionary_terms=("Max Mustermann",))
    scope = Scope(key="s1")
    adapter = FakeAdapter(reply="Hallo ⟦NAME_1⟧, alles klar.")
    outcome = await guarded_completion(
        profile=AIDER,
        purpose="code_completion",
        payload=EgressPayload(
            raw_text="Schreib Max Mustermann eine Mail",
            messages=[{"role": "user", "content": "Schreib Max Mustermann eine Mail"}],
        ),
        provider_target="claude",
        model="claude-sonnet-5",
        scope=scope,
        strategy=strategy,
        adapter=adapter,
        audit=AuditLog(),
    )
    assert outcome.released is True
    # Raus ging nur das Pseudonym …
    assert adapter.calls == [[{"role": "user", "content": "Schreib ⟦NAME_1⟧ eine Mail"}]]
    # … zurück kommt der echte Wert.
    assert outcome.response_text == "Hallo Max Mustermann, alles klar."


async def test_pseudonymizing_stream_reverses_token_split_over_chunks() -> None:
    # Pflicht-Fall Streaming-Holdback: Pseudonym über zwei Chunks (§7.2).
    strategy = PseudonymizingStrategy(dictionary_terms=("Max Mustermann",))
    scope = Scope(key="s2")
    adapter = FakeAdapter(stream_chunks=("Hallo ⟦NAM", "E_1⟧!"))
    outcome = await guarded_stream(
        profile=AIDER,
        purpose="code_completion",
        payload=EgressPayload(
            raw_text="Gruß an Max Mustermann",
            messages=[{"role": "user", "content": "Gruß an Max Mustermann"}],
        ),
        provider_target="claude",
        model="claude-sonnet-5",
        scope=scope,
        strategy=strategy,
        adapter=adapter,
        audit=AuditLog(),
    )
    assert outcome.released is True
    assert outcome.chunks is not None
    text = "".join([c async for c in outcome.chunks])
    assert text == "Hallo Max Mustermann!"


async def test_blocked_stream_never_touches_provider() -> None:
    adapter = FakeAdapter(stream_chunks=("x",))
    outcome = await guarded_stream(
        profile=None,
        purpose="code_completion",
        payload=EgressPayload(raw_text="x", messages=[{"role": "user", "content": "x"}]),
        provider_target="claude",
        model="m",
        adapter=adapter,
        audit=AuditLog(),
    )
    assert outcome.released is False
    assert outcome.chunks is None
    assert adapter.calls == []
