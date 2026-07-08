"""Service-Tests (Spec §7, Rev. 3) — ASGI in-process, kein Netz, Fake-Adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from sluice.audit import AuditLog
from sluice.policy import Profile, ReversibleConfig
from sluice.providers import ProviderResponse
from sluice.server import create_app

PROFILES = {
    "aider-code": Profile(
        name="aider-code",
        strategy="pseudonymizing",  # explizites Opt-in (Rev. 4)
        egress_enabled=True,
        allowed_purposes=("code_completion",),
        provider_allowlist=("claude", "openai"),
        detector_profile="infra",
        reversible=ReversibleConfig(),
    ),
    "temper": Profile(
        name="temper",
        strategy="generalizing",
        egress_enabled=True,
        allowed_purposes=("external_escalation",),
        provider_allowlist=("claude",),
        detector_profile="infra",
    ),
}


class FakeAdapter:
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


def _client(adapter: FakeAdapter | None = None) -> tuple[httpx.AsyncClient, FakeAdapter]:
    adapter = adapter or FakeAdapter()
    app = create_app(PROFILES, adapter_factory=lambda name: adapter, audit=AuditLog())
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://sluice"), adapter


BODY = {
    "messages": [{"role": "user", "content": "Mail an max@example.com schicken"}],
    "model": "claude-sonnet-5",
}


# ---------- /v1/health ----------


async def test_health() -> None:
    client, _ = _client()
    resp = await client.get("/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ---------- /v1/chat/completions: Default-Deny & Gate ----------


async def test_completions_without_profile_header_is_denied() -> None:
    client, adapter = _client()
    resp = await client.post("/v1/chat/completions", json=BODY)
    assert resp.status_code == 403
    assert "Default-Deny" in resp.json()["error"]["reason"]
    assert adapter.calls == []


async def test_completions_unknown_profile_is_denied() -> None:
    client, adapter = _client()
    resp = await client.post(
        "/v1/chat/completions", json=BODY, headers={"X-Sluice-Profile": "gibt-es-nicht"}
    )
    assert resp.status_code == 403
    assert adapter.calls == []


async def test_completions_provider_outside_allowlist_is_denied() -> None:
    client, adapter = _client()
    resp = await client.post(
        "/v1/chat/completions",
        json={**BODY, "provider": "gemini"},  # nicht in aider-codes Allowlist
        headers={"X-Sluice-Profile": "aider-code"},
    )
    assert resp.status_code == 403
    assert adapter.calls == []


# ---------- Opt-in reversibel: PII raus als Pseudonym, rein als Echtwert ----------


async def test_completions_pseudonymizing_profile_roundtrip() -> None:
    adapter = FakeAdapter(reply="Erledigt, ⟦EMAIL_1⟧ ist informiert.")
    client, _ = _client(adapter)
    resp = await client.post(
        "/v1/chat/completions",
        json=BODY,  # kein mode/purpose/provider — Profil-Strategie und Defaults greifen
        headers={"X-Sluice-Profile": "aider-code", "X-Sluice-Scope": "s-http-1"},
    )
    assert resp.status_code == 200
    # Der Provider sah nur das Pseudonym …
    assert adapter.calls == [[{"role": "user", "content": "Mail an ⟦EMAIL_1⟧ schicken"}]]
    # … der Konsument bekommt den echten Wert zurück.
    content = resp.json()["choices"][0]["message"]["content"]
    assert content == "Erledigt, max@example.com ist informiert."


async def test_completions_explicit_irreversible_mode_blocks_raw_pii() -> None:
    # Irreversibel generalisiert nicht selbst (Konsumenten-Domäne, §1.1) — roher
    # Identifier bleibt stehen und der Verifier blockt. Fail-closed.
    client, adapter = _client()
    resp = await client.post(
        "/v1/chat/completions",
        json={**BODY, "mode": "irreversible"},
        headers={"X-Sluice-Profile": "aider-code"},
    )
    assert resp.status_code == 403
    assert "Verifier blockiert" in resp.json()["error"]["reason"]
    assert adapter.calls == []


async def test_completions_unknown_mode_is_bad_request() -> None:
    client, adapter = _client()
    resp = await client.post(
        "/v1/chat/completions",
        json={**BODY, "mode": "plaintext"},
        headers={"X-Sluice-Profile": "aider-code"},
    )
    assert resp.status_code == 400
    assert adapter.calls == []


# ---------- Streaming (§7.2): Pseudonym über Chunk-Grenze ----------


async def test_completions_stream_reverses_across_chunks() -> None:
    adapter = FakeAdapter(stream_chunks=("Ok ⟦EMA", "IL_1⟧!"))
    client, _ = _client(adapter)
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={**BODY, "stream": True},
        headers={"X-Sluice-Profile": "aider-code", "X-Sluice-Scope": "s-http-2"},
    ) as resp:
        assert resp.status_code == 200
        body = "".join([chunk async for chunk in resp.aiter_text()])
    assert "max@example.com" in body
    assert "⟦" not in body.replace("data: [DONE]", "")
    assert body.rstrip().endswith("data: [DONE]")


# ---------- /v1/egress/guard (§7.1) ----------


async def test_guard_endpoint_releases_clean_generalized_text() -> None:
    client, _ = _client()
    resp = await client.post(
        "/v1/egress/guard",
        json={
            "profile": "temper",
            "purpose": "external_escalation",
            "raw_text": "Server db-prod-3 meldet OOM",
            "generalized_text": "Ein Server meldet Speicherdruck",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["released"] is True
    assert data["sanitized_text"] == "Ein Server meldet Speicherdruck"


async def test_guard_endpoint_blocks_identifier_with_200() -> None:
    client, _ = _client()
    resp = await client.post(
        "/v1/egress/guard",
        json={
            "profile": "temper",
            "purpose": "external_escalation",
            "raw_text": "Host 10.0.0.5 down",
            "generalized_text": "Host 10.0.0.5 down",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["released"] is False
    assert "Verifier blockiert" in data["reason"]
