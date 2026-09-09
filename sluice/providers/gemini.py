"""Google-Gemini-Adapter — generateContent-API (Spec §7.3).

role/content-Messages werden in Gemini-`contents` übersetzt (assistant → model,
system → systemInstruction); Streaming über `:streamGenerateContent?alt=sse`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from sluice.capacity import StreamTelemetry, usage_from_payload
from sluice.providers import (
    PROVIDER_TIMEOUT,
    ProviderError,
    ProviderResponse,
    ToolCall,
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

    def _body(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Neutrale Messages → Gemini-`contents` (§7.3).

        Tool-Turns (Rev. 15): ein `assistant.tool_calls` wird zu `functionCall`-Parts in
        einem `model`-Content, ein `role:"tool"`-Result zu einem `functionResponse`-Part
        in einem `user`-Content. **Aufeinanderfolgende Results einer Runde landen in EINEM
        Content** — wie bei Anthropic hängt der Konsumenten-Loop sie einzeln an.

        Gemini identifiziert ein Result über den **Namen**, die neutrale Form über die
        `tool_call_id`. Der Name wird deshalb aus den vorangegangenen `tool_calls`
        aufgelöst; lässt er sich nicht auflösen, ist das ein `ProviderError` — geraten
        wird nicht, ein falsch zugeordnetes Tool-Result ist schlimmer als ein Fehler.
        """
        contents: list[dict[str, Any]] = []
        system_parts: list[dict[str, str]] = []
        # tool_call_id → Tool-Name, gefüllt aus den Assistant-Turns dieses Verlaufs.
        names_by_id: dict[str, str] = {}
        pending_results: list[dict[str, Any]] = []

        def _flush() -> None:
            if pending_results:
                contents.append({"role": "user", "parts": list(pending_results)})
                pending_results.clear()

        for m in messages:
            role, content = m.get("role"), m.get("content")
            tool_calls = m.get("tool_calls")

            if role == "tool":
                call_id = m.get("tool_call_id", "")
                name = names_by_id.get(call_id)
                if name is None:
                    raise ProviderError(
                        f"gemini: Tool-Result für unbekannte tool_call_id '{call_id}' — "
                        f"Gemini adressiert Results über den Tool-Namen, der sich hier "
                        f"nicht auflösen lässt (fail-closed, §7.3)."
                    )
                pending_results.append(
                    {
                        "functionResponse": {
                            "name": name,
                            # Gemini erwartet ein Objekt; unser Result ist Text.
                            "response": {"result": content if content is not None else ""},
                        }
                    }
                )
                continue

            _flush()  # eine Nicht-Result-Message beendet einen Result-Lauf

            if role == "assistant" and tool_calls:
                parts: list[dict[str, Any]] = []
                if isinstance(content, str) and content:
                    parts.append({"text": content})
                for tc in tool_calls:
                    name = tc.get("name", "")
                    names_by_id[tc.get("id", "")] = name
                    parts.append({"functionCall": {"name": name, "args": tc.get("arguments") or {}}})
                contents.append({"role": "model", "parts": parts})
                continue

            if not isinstance(content, str):
                # Kein stilles Überspringen (§5.5): eine übersprungene Message käme beim
                # Modell nie an, obwohl sie die Boundary passiert hat — der Guard hätte
                # `released=true` für einen Inhalt protokolliert, den niemand sendet.
                # Der Adapter kann diese Form (noch) nicht — also fail-closed melden.
                raise ProviderError(
                    f"gemini: Message-Content vom Typ {type(content).__name__} wird von "
                    f"diesem Adapter nicht unterstützt (nur content:str) — fail-closed "
                    f"statt stillem Verwerfen (§5.5)."
                )
            if role == "system":
                system_parts.append({"text": content})
                continue
            contents.append(
                {"role": "model" if role == "assistant" else "user", "parts": [{"text": content}]}
            )
        _flush()

        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if system_parts:
            body["systemInstruction"] = {"parts": system_parts}
        if tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t["name"],
                            "description": t.get("description", ""),
                            "parameters": t.get("input_schema")
                            or {"type": "object", "properties": {}},
                        }
                        for t in tools
                    ]
                }
            ]
        return body

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        candidates = data.get("candidates") or []
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts", ())
        return "".join(p.get("text", "") for p in parts)

    @staticmethod
    def _extract_tool_calls(data: dict[str, Any]) -> tuple[ToolCall, ...]:
        """`functionCall`-Parts → neutrale ToolCalls (Rev. 15, §7.2).

        **Gemini vergibt keine Call-ID.** Sluice erzeugt sie deterministisch aus Name und
        Position, weil der neutrale Vertrag (und jeder Konsumenten-Loop) eine braucht, um
        das Result zuzuordnen. Der Konsument schickt genau diese ID im `role:"tool"`-Turn
        zurück, wo `_body` sie wieder zum Namen auflöst — der Kreis schließt sich in
        Sluice, ohne dass Gemini je eine ID sieht.
        """
        candidates = data.get("candidates") or []
        if not candidates:
            return ()
        parts = candidates[0].get("content", {}).get("parts", ())
        calls = []
        for i, part in enumerate(parts):
            fc = part.get("functionCall")
            if not fc:
                continue
            name = fc.get("name", "")
            calls.append(
                ToolCall(id=f"{name}-{i}", name=name, arguments=fc.get("args") or {})
            )
        return tuple(calls)

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
                f"{self._base_url}/v1beta/models/{model}:generateContent",
                headers=self._headers(),
                json=self._body(messages, max_tokens=max_tokens, tools=tools),
            )
            if resp.status_code != 200:
                raise ProviderError(f"gemini: HTTP {resp.status_code}: {resp.text[:500]}")
            data = resp.json()
            candidates = data.get("candidates") or []
            return ProviderResponse(
                text=self._extract_text(data),
                model=model,
                provider=self.name,
                tool_calls=self._extract_tool_calls(data),
                stop_reason=candidates[0].get("finishReason") if candidates else None,
                # Rev. 16 (§7.6): Gemini meldet den Verbrauch als `usageMetadata`, aber
                # KEINE Rate-Limit-Header. `rate_limit` bleibt deshalb None — unbekannt,
                # nicht „voll": die Auswahl (§4.5) darf daraus keinen Headroom ableiten.
                usage=usage_from_payload(data),
                rate_limit=None,
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
        """Text-Deltas; Verbrauch (Rev. 16, §7.6) landet in `telemetry`.

        Gemini wiederholt `usageMetadata` in jedem Chunk mit dem bis dahin erreichten
        Stand — der letzte gewinnt. Einen Rate-Limit-Kopfstand meldet die API nicht,
        `telemetry.rate_limit` bleibt daher leer.
        """
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
                    event = json.loads(line[5:].strip())
                    if telemetry is not None and event.get("usageMetadata"):
                        telemetry.usage = usage_from_payload(event)
                    chunk = self._extract_text(event)
                    if chunk:
                        yield chunk
        finally:
            if self._client is None:
                await client.aclose()
