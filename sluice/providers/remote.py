"""Remote-Gateway-Adapter — Sluice-Kern spricht einen eigenständigen Gateway-Service an
(Spec §7.3, Revision 6).

Die Provider-Gateways laufen als eigene Services (`sluice/gateway.py`) und können
jederzeit auf getrennte Server umziehen — der Kern kennt sie nur über ihre URL
(`SLUICE_GATEWAY_<PROVIDER>_URL`). Dieser Adapter implementiert dasselbe
`ProviderAdapter`-Protocol wie die direkten Adapter; der Dispatch ruft ihn wie
jeden anderen erst *nach* `released=true` (Invariante 2).

Interner Vertrag Kern → Gateway: `POST {base}/v1/complete`
(Body: messages/model/max_tokens[/stream]; optional Shared-Secret-Header
`X-Sluice-Gateway-Token` aus `SLUICE_GATEWAY_TOKEN`).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import structlog

from sluice.providers import (
    PROVIDER_TIMEOUT,
    ProviderConfigError,
    ProviderError,
    ProviderResponse,
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
        self, messages: list[dict[str, Any]], *, model: str, max_tokens: int = 1024
    ) -> ProviderResponse:
        client = self._client or httpx.AsyncClient(timeout=PROVIDER_TIMEOUT)
        try:
            resp = await client.post(
                f"{self._base_url}/v1/complete",
                headers=self._headers(),
                json={"messages": messages, "model": model, "max_tokens": max_tokens},
            )
            if resp.status_code != 200:
                self._raise_for_error(resp.status_code, resp.text)
            data = resp.json()
            return ProviderResponse(
                text=data.get("text", ""),
                model=data.get("model", model),
                provider=data.get("provider", self.name),
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
                    delta = json.loads(payload).get("delta", "")
                    if delta:
                        yield delta
        finally:
            if self._client is None:
                await client.aclose()
