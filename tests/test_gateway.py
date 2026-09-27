"""Gateway-Service-Tests (Spec §7.3, Rev. 6) — ASGI in-process, kein Netz, Fake-Adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from sluice.capacity import StreamTelemetry, Usage
from sluice.gateway import create_gateway_app
from sluice.providers import ProviderConfigError, ProviderError, ProviderResponse


class FakeAdapter:
    name = "fake"

    def __init__(self, *, reply: str = "ok", stream_chunks: tuple[str, ...] = ()) -> None:
        self.reply = reply
        self.stream_chunks = stream_chunks
        self.stream_usage: Usage | None = None
        self.calls: list[list[dict[str, Any]]] = []

    async def complete(
        self, messages: list[dict[str, Any]], *, model: str, max_tokens: int = 1024
    ) -> ProviderResponse:
        self.calls.append(messages)
        return ProviderResponse(text=self.reply, model=model, provider=self.name)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int = 1024,
        telemetry: StreamTelemetry | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append(messages)
        if telemetry is not None:
            telemetry.usage = self.stream_usage
        for chunk in self.stream_chunks:
            yield chunk


def _client(
    adapter: FakeAdapter | None = None, *, provider: str = "anthropic", token: str | None = None
) -> tuple[httpx.AsyncClient, FakeAdapter]:
    adapter = adapter or FakeAdapter()
    app = create_gateway_app(provider, adapter_factory=lambda name: adapter, token=token)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://gateway"), adapter


BODY = {"messages": [{"role": "user", "content": "Hallo ⟦NAME_1⟧"}], "model": "claude-sonnet-5"}


def test_gateway_without_provider_fails_closed() -> None:
    with pytest.raises(ProviderConfigError):
        create_gateway_app("")


async def test_health_reports_provider_with_canonical_alias() -> None:
    client, _ = _client(provider="claude")  # Alias → kanonisch "anthropic"
    resp = await client.get("/v1/health")
    assert resp.json() == {"status": "ok", "provider": "anthropic"}


async def test_complete_roundtrip() -> None:
    client, adapter = _client(FakeAdapter(reply="Hallo zurück"))
    resp = await client.post("/v1/complete", json=BODY)
    assert resp.status_code == 200
    assert resp.json() == {"text": "Hallo zurück", "model": "claude-sonnet-5", "provider": "fake"}
    assert adapter.calls == [BODY["messages"]]


async def test_stream_emits_delta_events_and_done() -> None:
    client, _ = _client(FakeAdapter(stream_chunks=("Hal", "lo")))
    async with client.stream("POST", "/v1/complete", json={**BODY, "stream": True}) as resp:
        assert resp.status_code == 200
        body = "".join([chunk async for chunk in resp.aiter_text()])
    assert 'data: {"delta": "Hal"}' in body
    assert body.rstrip().endswith("data: [DONE]")


async def test_token_enforced_when_configured() -> None:
    client, adapter = _client(token="s3cret")
    resp = await client.post("/v1/complete", json=BODY)
    assert resp.status_code == 401
    assert adapter.calls == []
    resp = await client.post(
        "/v1/complete", json=BODY, headers={"X-Sluice-Gateway-Token": "s3cret"}
    )
    assert resp.status_code == 200


async def test_missing_fields_are_bad_request() -> None:
    client, adapter = _client()
    resp = await client.post("/v1/complete", json={"model": "m"})
    assert resp.status_code == 400
    assert adapter.calls == []


class FailingAdapter(FakeAdapter):
    async def complete(
        self, messages: list[dict[str, Any]], *, model: str, max_tokens: int = 1024
    ) -> ProviderResponse:
        raise ProviderError("anthropic: HTTP 429: rate_limit_error", upstream_status=429)


async def test_upstream_error_is_logged_with_reason() -> None:
    # Der Provider-Fehlertext gehört ins Journal des Gateways, nicht nur in die Antwort.
    client, _ = _client(FailingAdapter())
    with capture_logs() as logs:
        resp = await client.post("/v1/complete", json=BODY)
    assert resp.status_code == 502
    [entry] = [e for e in logs if e["event"] == "gateway.provider_upstream"]
    assert entry["log_level"] == "warning"
    assert entry["provider"] == "anthropic"
    assert entry["model"] == "claude-sonnet-5"
    assert "HTTP 429" in entry["reason"]
    assert entry["upstream_status"] == 429
    assert resp.json()["error"]["upstream_status"] == 429


async def test_config_error_is_logged() -> None:
    def factory(name: str) -> FakeAdapter:
        raise ProviderConfigError("ANTHROPIC_API_KEY fehlt")

    app = create_gateway_app("anthropic", adapter_factory=factory)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway")
    with capture_logs() as logs:
        resp = await client.post("/v1/complete", json=BODY)
    assert resp.status_code == 500
    [entry] = [e for e in logs if e["event"] == "gateway.provider_config"]
    assert entry["log_level"] == "error"
    assert "ANTHROPIC_API_KEY" in entry["reason"]
