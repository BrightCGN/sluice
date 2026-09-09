"""Remote-Gateway-Adapter — Sluice-Kern spricht einen eigenständigen Gateway-Service an
(Spec §7.3, Revision 6).

Die Provider-Gateways laufen als eigene Services (`sluice/gateway.py`) und können
jederzeit auf getrennte Server umziehen — der Kern kennt sie nur über ihre URL
(`SLUICE_GATEWAY_<PROVIDER>_URL`). Dieser Adapter implementiert dasselbe
`ProviderAdapter`-Protocol wie die direkten Adapter; der Dispatch ruft ihn wie
jeden anderen erst *nach* `released=true` (Invariante 2).

Interner Vertrag Kern → Gateway: `POST {base}/v1/complete`
(Body: messages/model/max_tokens[/stream]; optional Shared-Secret-Header
`X-Sluice-Gateway-Token` aus `SLUICE_GATEWAY_TOKEN`). Seit Rev. 16 trägt die Antwort
zusätzlich `usage`/`rate_limit` (§7.6) — additiv, ein älteres Gateway lässt sie weg.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import structlog

from sluice.capacity import (
    StreamTelemetry,
    rate_limit_from_dict,
    usage_from_dict,
)
from sluice.providers import (
    PROVIDER_TIMEOUT,
    ProviderConfigError,
    ProviderError,
    ProviderResponse,
    ToolCall,
)

log = structlog.get_logger("sluice.providers.remote")


class RemoteGatewayAdapter:
    """Leitet Completions an einen Sluice-Gateway-Service weiter (§7.3, Rev. 6)."""

    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        token: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = provider
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = http_client

    def _headers(self) -> dict[str, str]:
        return {"X-Sluice-Gateway-Token": self._token} if self._token else {}

    def _raise_for_error(self, status: int, detail: str) -> None:
        # Konfigurationsfehler des Gateways (fehlender Key) bleiben als solche sichtbar —
        # fail-closed, kein stiller Fallback (§7.3).
        if status == 500 and "gateway_config" in detail:
            raise ProviderConfigError(f"gateway {self.name}: {detail[:500]}")
        raise ProviderError(f"gateway {self.name}: HTTP {status}: {detail[:500]}")

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int = 1024,
        tools: list[dict[str, Any]] | None = None,
    ) -> ProviderResponse:
        client = self._client or httpx.AsyncClient(timeout=PROVIDER_TIMEOUT)
        body: dict[str, Any] = {"messages": messages, "model": model, "max_tokens": max_tokens}
        # Rev. 14 (§7.2/§7.4): tools additiv an den internen Vertrag; fehlt es, ist der
        # Body byte-gleich zu Rev. 13 (ein Gateway ohne Rev.-14-Kenntnis bleibt kompatibel).
        if tools:
            body["tools"] = tools
        try:
            resp = await client.post(
                f"{self._base_url}/v1/complete",
                headers=self._headers(),
                json=body,
            )
            if resp.status_code != 200:
                self._raise_for_error(resp.status_code, resp.text)
            data = resp.json()
            tool_calls = tuple(
                ToolCall(
                    id=tc.get("id", ""),
                    name=tc.get("name", ""),
                    arguments=tc.get("arguments") or {},
                )
                for tc in data.get("tool_calls") or ()
            )
            return ProviderResponse(
                text=data.get("text", ""),
                model=data.get("model", model),
                provider=data.get("provider", self.name),
                tool_calls=tool_calls,
                stop_reason=data.get("stop_reason"),
                # Rev. 16 (§7.6): additiv. Ein Gateway ohne Rev.-16-Kenntnis sendet die
                # Felder nicht — dann bleibt der Stand unbekannt (None), und der Kern
                # verbucht den Aufruf trotzdem als Aufruf.
                usage=usage_from_dict(data.get("usage")),
                rate_limit=rate_limit_from_dict(data.get("rate_limit")),
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
        """Deltas vom Gateway; das **terminale** Event füllt `telemetry` (Rev. 16, §7.6).

        Das Gateway sendet vor `[DONE]` ein Event ohne `delta`, das Verbrauch und
        Kopfstand trägt. Ein Event ohne `delta` wurde schon vor Rev. 16 stillschweigend
        übergangen — deshalb ist der Vertrag in beide Richtungen verträglich.
        """
        client = self._client or httpx.AsyncClient(timeout=PROVIDER_TIMEOUT)
        try:
            async with client.stream(
                "POST",
                f"{self._base_url}/v1/complete",
                headers=self._headers(),
                json={
                    "messages": messages,
                    "model": model,
                    "max_tokens": max_tokens,
                    "stream": True,
                },
            ) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode(errors="replace")
                    self._raise_for_error(resp.status_code, detail)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    event = json.loads(payload)
                    delta = event.get("delta", "")
                    if delta:
                        yield delta
                        continue
                    if telemetry is not None:
                        if event.get("usage"):
                            telemetry.usage = usage_from_dict(event["usage"])
                        if event.get("rate_limit"):
                            telemetry.rate_limit = rate_limit_from_dict(event["rate_limit"])
        finally:
            if self._client is None:
                await client.aclose()
