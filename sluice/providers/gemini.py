"""Google-Gemini-Adapter — generateContent-API (Spec §7.3).

role/content-Messages werden in Gemini-`contents` übersetzt (assistant → model,
system → systemInstruction); Streaming über `:streamGenerateContent?alt=sse`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from sluice.providers import (
    PROVIDER_TIMEOUT,
    ProviderError,
    ProviderResponse,
    require_api_key,
)


class GeminiAdapter:
    name = "gemini"

    def __init__(
        self,
        *,
        base_url: str = "https://generativelanguage.googleapis.com",
        api_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = http_client

    def _headers(self) -> dict[str, str]:
        key = self._api_key or require_api_key("SLUICE_GEMINI_API_KEY", self.name)
        return {"x-goog-api-key": key}

    def _body(self, messages: list[dict[str, Any]], *, max_tokens: int) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        system_parts: list[dict[str, str]] = []
        for m in messages:
            role, content = m.get("role"), m.get("content")
            if not isinstance(content, str):
                continue
            if role == "system":
                system_parts.append({"text": content})
                continue
            contents.append(
                {"role": "model" if role == "assistant" else "user", "parts": [{"text": content}]}
            )
        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if system_parts:
            body["systemInstruction"] = {"parts": system_parts}
        return body

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        candidates = data.get("candidates") or []
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts", ())
        return "".join(p.get("text", "") for p in parts)

    async def complete(
        self, messages: list[dict[str, Any]], *, model: str, max_tokens: int = 1024
    ) -> ProviderResponse:
        client = self._client or httpx.AsyncClient(timeout=PROVIDER_TIMEOUT)
        try:
            resp = await client.post(
                f"{self._base_url}/v1beta/models/{model}:generateContent",
                headers=self._headers(),
                json=self._body(messages, max_tokens=max_tokens),
            )
            if resp.status_code != 200:
                raise ProviderError(f"gemini: HTTP {resp.status_code}: {resp.text[:500]}")
            return ProviderResponse(
                text=self._extract_text(resp.json()), model=model, provider=self.name
            )
        finally:
            if self._client is None:
                await client.aclose()

    async def stream(
        self, messages: list[dict[str, Any]], *, model: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        client = self._client or httpx.AsyncClient(timeout=PROVIDER_TIMEOUT)
        try:
            async with client.stream(
                "POST",
                f"{self._base_url}/v1beta/models/{model}:streamGenerateContent?alt=sse",
                headers=self._headers(),
                json=self._body(messages, max_tokens=max_tokens),
            ) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode(errors="replace")[:500]
                    raise ProviderError(f"gemini: HTTP {resp.status_code}: {detail}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = self._extract_text(json.loads(line[5:].strip()))
                    if chunk:
                        yield chunk
        finally:
            if self._client is None:
                await client.aclose()
