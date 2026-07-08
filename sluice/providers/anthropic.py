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

from sluice.providers import (
    PROVIDER_TIMEOUT,
    ProviderError,
    ProviderResponse,
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
        self, messages: list[dict[str, Any]], *, model: str, max_tokens: int
    ) -> dict[str, Any]:
        system_parts = [
            m["content"] for m in messages if m.get("role") == "system" and m.get("content")
        ]
        chat = [m for m in messages if m.get("role") != "system"]
        body: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": chat}
        if system_parts:
            body["system"] = "\n".join(system_parts)
        return body

    async def complete(
        self, messages: list[dict[str, Any]], *, model: str, max_tokens: int = 1024
    ) -> ProviderResponse:
        client = self._client or httpx.AsyncClient(timeout=PROVIDER_TIMEOUT)
        try:
            resp = await client.post(
                f"{self._base_url}/v1/messages",
                headers=self._headers(),
                json=self._body(messages, model=model, max_tokens=max_tokens),
            )
            if resp.status_code != 200:
                raise ProviderError(f"anthropic: HTTP {resp.status_code}: {resp.text[:500]}")
            data = resp.json()
            text = "".join(
                block.get("text", "")
                for block in data.get("content", ())
                if block.get("type") == "text"
            )
            return ProviderResponse(text=text, model=data.get("model", model), provider=self.name)
        finally:
            if self._client is None:
                await client.aclose()

    async def stream(
        self, messages: list[dict[str, Any]], *, model: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
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
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    event = json.loads(line[5:].strip())
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield delta.get("text", "")
        finally:
            if self._client is None:
                await client.aclose()
