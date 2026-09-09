"""Anthropic-Adapter (Claude) — Messages-API (Spec §7.3).

Wird nur nach `released=true` aufgerufen; sieht ausschließlich sanitisierte
Messages. System-Messages werden in den `system`-Parameter gehoben (API-Anforderung).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import structlog

from sluice.capacity import (
    StreamTelemetry,
    Usage,
    rate_limit_from_headers,
    usage_from_payload,
)
from sluice.providers import (
    PROVIDER_TIMEOUT,
    ProviderError,
    ProviderResponse,
    ToolCall,
    require_api_key,
)

log = structlog.get_logger("sluice.providers.anthropic")

_API_VERSION = "2023-06-01"


class AnthropicAdapter:
    name = "anthropic"

    def __init__(
        self,
        *,
        base_url: str = "https://api.anthropic.com",
        api_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = http_client

    def _headers(self) -> dict[str, str]:
        key = self._api_key or require_api_key("SLUICE_ANTHROPIC_API_KEY", self.name)
        return {"x-api-key": key, "anthropic-version": _API_VERSION}

    def _body(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        system_parts = [
            m["content"] for m in messages if m.get("role") == "system" and m.get("content")
        ]
        chat = self._to_native_messages(messages)
        body: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": chat}
        if system_parts:
            body["system"] = "\n".join(system_parts)
        # Rev. 14 (§7.2): die neutrale Tool-Spec `{name, description, input_schema}` ist
        # deckungsgleich mit Anthropics nativem Tool-Schema — kein Umbau nötig, nur reichen.
        if tools:
            body["tools"] = tools
        return body

    @staticmethod
    def _to_native_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Neutrale Tool-Turn-Messages → Anthropic-Content-Blöcke (Rev. 14, §7.2).

        Der Konsument spricht die neutrale/OpenAI-nahe Form (Rolle `tool`,
        `assistant.tool_calls`); Anthropic kennt nur `tool_use`/`tool_result` als
        Content-Blöcke. Plain-Text-Messages (nur `role`/`content:str`) bleiben
        unverändert — der Nicht-Tool-Pfad ist byte-gleich zu Rev. 13.

        **Aufeinanderfolgende Tool-Results einer Runde werden zu EINER `user`-Nachricht
        zusammengefasst** (Anthropic verlangt alle `tool_result`-Blöcke eines Turns in
        einer Nachricht; der Hub-Loop hängt sie einzeln an).
        """
        out: list[dict[str, Any]] = []
        pending_results: list[dict[str, Any]] = []

        def _flush() -> None:
            if pending_results:
                out.append({"role": "user", "content": list(pending_results)})
                pending_results.clear()

        for m in messages:
            role = m.get("role")
            if role == "system":
                continue  # geht in den system-Parameter (oben)
            if role == "tool":
                pending_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": m.get("tool_call_id", ""),
                        "content": m.get("content", ""),
                    }
                )
                continue
            _flush()  # eine Nicht-Tool-Message beendet einen tool_result-Lauf
            tool_calls = m.get("tool_calls")
            if role == "assistant" and tool_calls:
                blocks: list[dict[str, Any]] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in tool_calls:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": tc.get("name", ""),
                            "input": tc.get("arguments") or {},
                        }
                    )
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": role, "content": m.get("content", "")})
        _flush()
        return out

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int = 1024,
        tools: list[dict[str, Any]] | None = None,
    ) -> ProviderResponse:
        client = self._client or httpx.AsyncClient(timeout=PROVIDER_TIMEOUT)
        try:
            resp = await client.post(
                f"{self._base_url}/v1/messages",
                headers=self._headers(),
                json=self._body(messages, model=model, max_tokens=max_tokens, tools=tools),
            )
            if resp.status_code != 200:
                raise ProviderError(f"anthropic: HTTP {resp.status_code}: {resp.text[:500]}")
            data = resp.json()
            text = "".join(
                block.get("text", "")
                for block in data.get("content", ())
                if block.get("type") == "text"
            )
            # Rev. 14: `tool_use`-Blöcke → neutrale ToolCalls. `input` ist bereits ein
            # Objekt (echte Werte), der Konsument führt das Tool lokal darauf aus.
            tool_calls = tuple(
                ToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=block.get("input") or {},
                )
                for block in data.get("content", ())
                if block.get("type") == "tool_use"
            )
            return ProviderResponse(
                text=text,
                model=data.get("model", model),
                provider=self.name,
                tool_calls=tool_calls,
                stop_reason=data.get("stop_reason"),
                # Rev. 16 (§7.6): Verbrauch aus dem Body, Kopfstand aus den Headern.
                # Beides additiv — fehlt es, bleibt es None (unbekannt, nicht null).
                usage=usage_from_payload(data),
                rate_limit=rate_limit_from_headers(resp.headers, prefix="anthropic"),
            )
        finally:
            if self._client is None:
                await client.aclose()

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int = 1024,
        telemetry: StreamTelemetry | None = None,
    ) -> AsyncIterator[str]:
        """Text-Deltas; Kapazitätsdaten (Rev. 16, §7.6) landen in `telemetry`.

        Anthropic verteilt den Verbrauch über zwei Events: `message_start` trägt die
        Eingabe-Tokens, `message_delta` die bis dahin erzeugten Ausgabe-Tokens. Beide
        werden zusammengeführt, damit die Senke am Ende einen vollständigen Wert hält
        und nicht die Hälfte — eine halbe Messung sähe im Snapshot aus wie eine ganze.
        """
        client = self._client or httpx.AsyncClient(timeout=PROVIDER_TIMEOUT)
        body = self._body(messages, model=model, max_tokens=max_tokens)
        body["stream"] = True
        try:
            async with client.stream(
                "POST", f"{self._base_url}/v1/messages", headers=self._headers(), json=body
            ) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode(errors="replace")[:500]
                    raise ProviderError(f"anthropic: HTTP {resp.status_code}: {detail}")
                if telemetry is not None:
                    telemetry.rate_limit = rate_limit_from_headers(
                        resp.headers, prefix="anthropic"
                    )
                seen = Usage()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    event = json.loads(line[5:].strip())
                    kind = event.get("type")
                    if kind == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield delta.get("text", "")
                    elif telemetry is not None and kind in ("message_start", "message_delta"):
                        block = (event.get("message") or event).get("usage")
                        if isinstance(block, dict):
                            seen = Usage(
                                input_tokens=int(block.get("input_tokens") or 0)
                                or seen.input_tokens,
                                output_tokens=int(block.get("output_tokens") or 0)
                                or seen.output_tokens,
                            )
                            telemetry.usage = seen
        finally:
            if self._client is None:
                await client.aclose()
