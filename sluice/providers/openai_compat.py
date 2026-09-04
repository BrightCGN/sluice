"""Gemeinsamer Chat-Completions-Dialekt — Basis für OpenAI und Mistral (Spec §7.3).

Beide Provider sprechen dasselbe `/v1/chat/completions`-Format (Bearer-Auth,
`choices[0].message.content`, SSE-Deltas mit `[DONE]`-Terminator). Die Subklassen
setzen nur Name, Basis-URL und Key-Env.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from sluice.dialect import (
    DialectError,
    normalize_tool_calls,
    to_openai_messages,
    to_openai_tools,
)
from sluice.providers import (
    PROVIDER_TIMEOUT,
    ProviderError,
    ProviderResponse,
    ToolCall,
    require_api_key,
)


class OpenAICompatAdapter:
    name: str = "openai-compat"
    key_env: str = ""
    default_base_url: str = ""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (base_url or self.default_base_url).rstrip("/")
        self._api_key = api_key
        self._client = http_client

    def _headers(self) -> dict[str, str]:
        key = self._api_key or require_api_key(self.key_env, self.name)
        return {"Authorization": f"Bearer {key}"}

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int = 1024,
        tools: list[dict[str, Any]] | None = None,
    ) -> ProviderResponse:
        client = self._client or httpx.AsyncClient(timeout=PROVIDER_TIMEOUT)
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            # Neutral → nativ (§7.3). Für diese Provider-Familie ist „nativ" der
            # OpenAI-Dialekt, deshalb dieselbe Abbildung wie am Endpoint (§7.2) statt
            # einer zweiten, die auseinanderlaufen könnte.
            "messages": to_openai_messages(messages),
        }
        if tools:
            body["tools"] = to_openai_tools(tools)
        try:
            resp = await client.post(
                f"{self._base_url}/chat/completions",
                headers=self._headers(),
                json=body,
            )
            if resp.status_code != 200:
                raise ProviderError(f"{self.name}: HTTP {resp.status_code}: {resp.text[:500]}")
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                raise ProviderError(f"{self.name}: Antwort ohne choices.")
            message = choices[0].get("message", {})
            text = message.get("content") or ""
            # Native Tool-Calls → neutrale ToolCalls. `function.arguments` ist ein
            # JSON-String; ein unparsebarer wäre ein Vertragsbruch des Providers und
            # wird gemeldet, nicht als leeres Argument-Objekt beschönigt.
            raw_calls = message.get("tool_calls") or ()
            try:
                parsed = normalize_tool_calls(list(raw_calls), f"{self.name}.tool_calls")
            except DialectError as exc:
                raise ProviderError(f"{self.name}: {exc}") from None
            return ProviderResponse(
                text=text,
                model=data.get("model", model),
                provider=self.name,
                tool_calls=tuple(
                    ToolCall(id=c["id"], name=c["name"], arguments=c["arguments"])
                    for c in parsed
                ),
                stop_reason=choices[0].get("finish_reason"),
            )
        finally:
            if self._client is None:
                await client.aclose()

    async def stream(
        self, messages: list[dict[str, Any]], *, model: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        client = self._client or httpx.AsyncClient(timeout=PROVIDER_TIMEOUT)
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": to_openai_messages(messages),
            "stream": True,
        }
        try:
            async with client.stream(
                "POST", f"{self._base_url}/chat/completions", headers=self._headers(), json=body
            ) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode(errors="replace")[:500]
                    raise ProviderError(f"{self.name}: HTTP {resp.status_code}: {detail}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    event = json.loads(payload)
                    choices = event.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta", {}).get("content")
                        if delta:
                            yield delta
        finally:
            if self._client is None:
                await client.aclose()
