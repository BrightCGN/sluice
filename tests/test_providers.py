"""Provider-Adapter-Tests (Spec §7.3) — kein Netz, httpx.MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest

from sluice.providers import ProviderConfigError, ProviderError, select_provider
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


async def test_openai_sends_max_completion_tokens_not_max_tokens() -> None:
    """Die GPT-5-Familie weist `max_tokens` mit HTTP 400 zurueck (Rev. 17, §7.3).

    Beobachtet im Betrieb, nachdem das Guthaben stand:
      "Unsupported parameter: 'max_tokens' is not supported with this model.
       Use 'max_completion_tokens' instead."
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"model": "gpt-x", "choices": [{"message": {"content": "OK"}}]}
        )

    adapter = OpenAIAdapter(api_key="k", http_client=_client(handler))
    await adapter.complete(MESSAGES, model="gpt-x", max_tokens=16000)
    assert seen["body"]["max_completion_tokens"] == 16000
    assert "max_tokens" not in seen["body"]      # sonst quittiert OpenAI mit 400


async def test_openai_stream_uses_the_same_field() -> None:
    """Zwei Pfade, ein Dialekt — sonst geht nur einer von beiden kaputt."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _sse(json.dumps({"choices": [{"delta": {"content": "OK"}}]}), "[DONE]")

    adapter = OpenAIAdapter(api_key="k", http_client=_client(handler))
    [c async for c in adapter.stream(MESSAGES, model="gpt-x", max_tokens=16000)]
    assert seen["body"]["max_completion_tokens"] == 16000
    assert "max_tokens" not in seen["body"]


async def test_mistral_keeps_max_tokens() -> None:
    """Der Dialekt ist geteilt, das Feld nicht: Mistral nimmt weiterhin max_tokens.

    Deshalb sitzt der Feldname in der Subklasse und nicht in einer
    Fallunterscheidung nach Modellnamen in der Basis.
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"model": "m", "choices": [{"message": {"content": "OK"}}]}
        )

    adapter = MistralAdapter(api_key="k", http_client=_client(handler))
    await adapter.complete(MESSAGES, model="mistral-large-latest", max_tokens=16000)
    assert seen["body"]["max_tokens"] == 16000
    assert "max_completion_tokens" not in seen["body"]


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
        base_url="http://192.0.2.10:17890",
        token="s3cret",
        http_client=_client(handler),
    )
    resp = await adapter.complete(MESSAGES, model="claude-sonnet-5")
    assert resp.text == "Hallo zurück"
    assert resp.provider == "anthropic"
    assert seen["url"] == "http://192.0.2.10:17890/v1/complete"
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

    monkeypatch.setenv("SLUICE_GATEWAY_ANTHROPIC_URL", "http://192.0.2.10:17890")
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


# ---------- upstream_status (§7.4): der Status des Providers, nie geraten ----------


@pytest.mark.parametrize(
    "make",
    [
        lambda c: AnthropicAdapter(api_key="k", http_client=c),
        lambda c: OpenAIAdapter(api_key="k", http_client=c),
        lambda c: MistralAdapter(api_key="k", http_client=c),
        lambda c: GeminiAdapter(api_key="k", http_client=c),
    ],
)
async def test_adapter_error_carries_upstream_status(make) -> None:
    adapter = make(_client(lambda r: httpx.Response(503, text="high demand")))
    with pytest.raises(ProviderError) as info:
        await adapter.complete(MESSAGES, model="m")
    assert info.value.upstream_status == 503


@pytest.mark.parametrize(
    "make",
    [
        lambda c: AnthropicAdapter(api_key="k", http_client=c),
        lambda c: OpenAIAdapter(api_key="k", http_client=c),
        lambda c: GeminiAdapter(api_key="k", http_client=c),
    ],
)
async def test_adapter_stream_error_carries_upstream_status(make) -> None:
    adapter = make(_client(lambda r: httpx.Response(429, text="slow down")))
    with pytest.raises(ProviderError) as info:
        async for _ in adapter.stream(MESSAGES, model="m"):
            pass
    assert info.value.upstream_status == 429


async def test_remote_gateway_passes_provider_status_not_its_own() -> None:
    from sluice.providers.remote import RemoteGatewayAdapter

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            json={"error": {"type": "gateway_upstream", "reason": "x", "upstream_status": 503}},
        )

    adapter = RemoteGatewayAdapter(
        provider="gemini", base_url="http://gw:17892", http_client=_client(handler)
    )
    with pytest.raises(ProviderError) as info:
        await adapter.complete(MESSAGES, model="m")
    assert info.value.upstream_status == 503


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(502, json={"error": {"type": "gateway_upstream", "reason": "x"}}),
        httpx.Response(502, text="Bad Gateway"),
        httpx.Response(502, json={"error": {"upstream_status": "503"}}),
    ],
)
async def test_remote_gateway_without_status_field_reports_none(response) -> None:
    # Älteres Gateway, Proxy-Fehlerseite, Unsinn im Feld: unbekannt bleibt unbekannt —
    # insbesondere nicht die 502 des Gateways als Provider-Status ausgeben.
    from sluice.providers.remote import RemoteGatewayAdapter

    adapter = RemoteGatewayAdapter(
        provider="gemini", base_url="http://gw:17892", http_client=_client(lambda r: response)
    )
    with pytest.raises(ProviderError) as info:
        await adapter.complete(MESSAGES, model="m")
    assert info.value.upstream_status is None


# --- Lokaler llama-server (§7.3) --------------------------------------------
#
# Ein Modell im eigenen Netz ist kein Egress — es laeuft trotzdem durch Sluice,
# damit es EINEN Weg zum Modell gibt. Die beiden Abweichungen von den
# Cloud-Adaptern sind Absicht und werden hier festgehalten.


def test_llamacpp_without_a_base_url_is_fail_closed(monkeypatch):
    """Kein geratener Default: `localhost` waere die Gateway-VM, nicht das Modell.

    Ein solcher Default haette auf die falsche Maschine gezeigt, und der Fehler
    saehe wie ein Netzproblem aus.
    """
    monkeypatch.delenv("SLUICE_LLAMACPP_BASE_URL", raising=False)
    with pytest.raises(ProviderConfigError, match="SLUICE_LLAMACPP_BASE_URL"):
        select_provider("llamacpp")


def test_llamacpp_takes_its_base_url_from_the_env(monkeypatch):
    monkeypatch.setenv("SLUICE_LLAMACPP_BASE_URL", "http://192.0.2.50:8080/v1/")
    adapter = select_provider("llamacpp")
    assert adapter._base_url == "http://192.0.2.50:8080/v1"      # ohne Schraegstrich


def test_llamacpp_demands_a_key(monkeypatch):
    """`llama-server --api-key …` ist der Normalfall und wird erzwungen.

    Ein offener Inferenz-Endpunkt im LAN ist einer, auf dem jeder Gast Modelle
    laufen laesst — stillschweigend ohne Key zu senden waere die Bequemlichkeit,
    die man erst bemerkt, wenn sie ausgenutzt wird.
    """
    monkeypatch.setenv("SLUICE_LLAMACPP_BASE_URL", "http://192.0.2.50:8080/v1")
    monkeypatch.delenv("SLUICE_LLAMACPP_API_KEY", raising=False)
    monkeypatch.delenv("SLUICE_LLAMACPP_ALLOW_NO_AUTH", raising=False)
    adapter = select_provider("llamacpp")
    with pytest.raises(ProviderConfigError, match="ALLOW_NO_AUTH"):
        adapter._headers()


def test_llamacpp_runs_without_a_key_only_after_an_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("SLUICE_LLAMACPP_BASE_URL", "http://192.0.2.50:8080/v1")
    monkeypatch.delenv("SLUICE_LLAMACPP_API_KEY", raising=False)
    monkeypatch.setenv("SLUICE_LLAMACPP_ALLOW_NO_AUTH", "1")
    assert select_provider("llamacpp")._headers() == {}


def test_llamacpp_sends_a_bearer_header_like_openai(monkeypatch):
    monkeypatch.setenv("SLUICE_LLAMACPP_BASE_URL", "http://192.0.2.50:8080/v1")
    monkeypatch.setenv("SLUICE_LLAMACPP_API_KEY", "geheim")
    assert select_provider("llamacpp")._headers() == {"Authorization": "Bearer geheim"}


def test_llamacpp_is_not_tool_capable():
    """llama.cpp traegt Tool-Calling je Modell und Chat-Template verschieden.

    Ein Dialekt, der nur manchmal funktioniert, ist schlechter als keiner.
    """
    from sluice.providers import TOOL_CAPABLE_PROVIDERS

    assert "llamacpp" not in TOOL_CAPABLE_PROVIDERS


def test_every_canonical_provider_has_an_adapter(monkeypatch) -> None:
    """Kern-Liste und Adapter-Zuordnung muessen sich in BEIDEN Richtungen decken.

    Es gibt zwei Stellen: `select_provider` baut den Adapter im Gateway,
    `select_egress_adapter` prueft im Kern gegen CANONICAL_PROVIDERS. Am
    05.10.2026 lief das auseinander — llamacpp war in der Registry, nicht in
    CANONICAL_PROVIDERS. Das Gateway startete tadellos, der Kern wies den Provider
    mit "kein Adapter registriert" ab: eine Meldung, die auf einen fehlenden
    Adapter zeigt, obwohl er dalag.

    Der erste Anlauf dieses Tests lief ueber CANONICAL_PROVIDERS und merkte
    deshalb NICHTS, wenn dort ein Eintrag fehlte — eine Tautologie. Geprueft wird
    jetzt die Zuordnung _MODULES/_CLASSES gegen die Liste, in beide Richtungen.
    """
    from sluice.providers import _CLASSES, _MODULES, CANONICAL_PROVIDERS

    assert set(_MODULES) == set(CANONICAL_PROVIDERS)
    assert set(_CLASSES) == set(CANONICAL_PROVIDERS)

    monkeypatch.setenv("SLUICE_LLAMACPP_BASE_URL", "http://192.0.2.50:8080/v1")
    for name in CANONICAL_PROVIDERS:
        assert select_provider(name).name == name, f"{name}: Adapter fehlt"


def test_the_core_accepts_every_canonical_provider(monkeypatch) -> None:
    """Und der Kern-Pfad nimmt dieselben Namen an (Gateway-URL vorausgesetzt)."""
    from sluice.providers import CANONICAL_PROVIDERS, select_egress_adapter

    for name in CANONICAL_PROVIDERS:
        monkeypatch.setenv(f"SLUICE_GATEWAY_{name.upper()}_URL", "http://127.0.0.1:1")
        select_egress_adapter(name)      # darf nicht werfen


def test_an_alias_resolves_to_its_canonical_adapter(monkeypatch) -> None:
    """„claude" ist ein Alias auf anthropic (§4) — beide Pfade muessen ihn kennen."""
    from sluice.providers import select_egress_adapter

    assert select_provider("claude").name == "anthropic"
    monkeypatch.setenv("SLUICE_GATEWAY_ANTHROPIC_URL", "http://127.0.0.1:1")
    select_egress_adapter("claude")
