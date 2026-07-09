"""Provider-Adapter-Tests (Spec §7.3) — kein Netz, httpx.MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest

from sluice.providers import ProviderConfigError, select_provider
from sluice.providers.anthropic import AnthropicAdapter
from sluice.providers.gemini import GeminiAdapter
from sluice.providers.mistral import MistralAdapter
from sluice.providers.openai import OpenAIAdapter

MESSAGES = [
    {"role": "system", "content": "Sei knapp."},
    {"role": "user", "content": "Hallo ⟦NAME_1⟧"},
]


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _sse(*lines: str) -> httpx.Response:
    return httpx.Response(
        200,
        content="".join(f"data: {line}\n\n" for line in lines).encode(),
        headers={"content-type": "text/event-stream"},
    )


# ---------- Registry (§7.3) ----------


def test_select_provider_unknown_fails_closed() -> None:
    with pytest.raises(ProviderConfigError, match="Unbekannter Provider"):
        select_provider("acme-llm")


def test_select_provider_claude_alias() -> None:
    assert isinstance(select_provider("claude"), AnthropicAdapter)


def test_missing_api_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLUICE_OPENAI_API_KEY", raising=False)
    adapter = OpenAIAdapter(http_client=_client(lambda r: httpx.Response(200)))
    with pytest.raises(ProviderConfigError, match="SLUICE_OPENAI_API_KEY"):
        adapter._headers()


# ---------- Anthropic ----------


async def test_anthropic_complete_hoists_system_and_extracts_text() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["key"] = request.headers.get("x-api-key")
        return httpx.Response(
            200,
            json={
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": "Hallo zurück"}],
            },
        )

    adapter = AnthropicAdapter(api_key="k-test", http_client=_client(handler))
    resp = await adapter.complete(MESSAGES, model="claude-sonnet-5")
    assert resp.text == "Hallo zurück"
    assert resp.provider == "anthropic"
    assert seen["url"].endswith("/v1/messages")
    assert seen["key"] == "k-test"
    assert seen["body"]["system"] == "Sei knapp."
    assert all(m["role"] != "system" for m in seen["body"]["messages"])


async def test_anthropic_stream_yields_text_deltas() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _sse(
            json.dumps({"type": "message_start"}),
            json.dumps(
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hal"}}
            ),
            json.dumps(
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}}
            ),
            json.dumps({"type": "message_stop"}),
        )

    adapter = AnthropicAdapter(api_key="k", http_client=_client(handler))
    chunks = [c async for c in adapter.stream(MESSAGES, model="m")]
    assert chunks == ["Hal", "lo"]


# ---------- OpenAI / Mistral (gemeinsamer Dialekt) ----------


async def test_openai_complete() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={"model": "gpt-x", "choices": [{"message": {"content": "Antwort"}}]},
        )

    adapter = OpenAIAdapter(api_key="k-oai", http_client=_client(handler))
    resp = await adapter.complete(MESSAGES, model="gpt-x")
    assert resp.text == "Antwort"
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    assert seen["auth"] == "Bearer k-oai"


async def test_mistral_stream_stops_at_done() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "api.mistral.ai" in str(request.url)
        return _sse(
            json.dumps({"choices": [{"delta": {"content": "Bon"}}]}),
            json.dumps({"choices": [{"delta": {"content": "jour"}}]}),
            "[DONE]",
        )

    adapter = MistralAdapter(api_key="k", http_client=_client(handler))
    chunks = [c async for c in adapter.stream(MESSAGES, model="mistral-large")]
    assert chunks == ["Bon", "jour"]


# ---------- Gemini ----------


async def test_gemini_complete_translates_roles() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "Ergebnis"}]}}]},
        )

    adapter = GeminiAdapter(api_key="k-gem", http_client=_client(handler))
    messages = MESSAGES + [{"role": "assistant", "content": "vorher"}]
    resp = await adapter.complete(messages, model="gemini-pro")
    assert resp.text == "Ergebnis"
    assert "models/gemini-pro:generateContent" in seen["url"]
    assert seen["body"]["systemInstruction"] == {"parts": [{"text": "Sei knapp."}]}
    roles = [c["role"] for c in seen["body"]["contents"]]
    assert roles == ["user", "model"]


# ---------- Remote-Gateway (§7.3, Rev. 6): Kern → eigenständiger Gateway-Service ----------


async def test_remote_gateway_complete_forwards_and_sends_token() -> None:
    from sluice.providers.remote import RemoteGatewayAdapter

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["token"] = request.headers.get("X-Sluice-Gateway-Token")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"text": "Hallo zurück", "model": "claude-sonnet-5", "provider": "anthropic"}
        )

    adapter = RemoteGatewayAdapter(
        provider="anthropic",
        base_url="http://192.168.87.40:17890",
        token="s3cret",
        http_client=_client(handler),
    )
    resp = await adapter.complete(MESSAGES, model="claude-sonnet-5")
    assert resp.text == "Hallo zurück"
    assert resp.provider == "anthropic"
    assert seen["url"] == "http://192.168.87.40:17890/v1/complete"
    assert seen["token"] == "s3cret"
    assert seen["body"]["messages"] == MESSAGES


async def test_remote_gateway_stream_parses_deltas_until_done() -> None:
    from sluice.providers.remote import RemoteGatewayAdapter

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse('{"delta": "Hal"}', '{"delta": "lo"}', "[DONE]")

    adapter = RemoteGatewayAdapter(
        provider="openai", base_url="http://gw:17891", http_client=_client(handler)
    )
    chunks = [c async for c in adapter.stream(MESSAGES, model="gpt")]
    assert chunks == ["Hal", "lo"]


async def test_remote_gateway_config_error_stays_fail_closed() -> None:
    from sluice.providers.remote import RemoteGatewayAdapter

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500, json={"error": {"type": "gateway_config", "reason": "Key fehlt"}}
        )

    adapter = RemoteGatewayAdapter(
        provider="mistral", base_url="http://gw:17893", http_client=_client(handler)
    )
    with pytest.raises(ProviderConfigError, match="gateway_config"):
        await adapter.complete(MESSAGES, model="m")


def test_select_egress_adapter_requires_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rev. 7: der Kern kennt nur Gateway-Adapter — ohne URL fail-closed, nie direkt."""
    from sluice.providers import select_egress_adapter
    from sluice.providers.remote import RemoteGatewayAdapter

    monkeypatch.setenv("SLUICE_GATEWAY_ANTHROPIC_URL", "http://192.168.87.40:17890")
    adapter = select_egress_adapter("claude")  # Alias → kanonisch "anthropic"
    assert isinstance(adapter, RemoteGatewayAdapter)
    assert adapter.name == "anthropic"

    # Fehlende Gateway-URL ⇒ Konfigurationsfehler, KEIN Fallback auf den direkten Adapter.
    monkeypatch.delenv("SLUICE_GATEWAY_ANTHROPIC_URL")
    with pytest.raises(ProviderConfigError, match="SLUICE_GATEWAY_ANTHROPIC_URL"):
        select_egress_adapter("claude")

    # Unbekannter Provider bleibt ebenfalls fail-closed.
    with pytest.raises(ProviderConfigError, match="Unbekannter Provider"):
        select_egress_adapter("acme-llm")
