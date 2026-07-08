"""Sluice Provider-Gateway — eigenständiger Service pro Provider (Spec §7.3, Revision 6).

Ein Gateway kapselt genau EINEN Provider-Adapter als eigenen Prozess mit eigenem
Lebenszyklus — dadurch können Sluice-Kern und Gateways jederzeit auf getrennte
Server umziehen; der Kern kennt ein Gateway nur über `SLUICE_GATEWAY_<PROVIDER>_URL`.

Das Gateway ist ein INTERNER Dienst hinter der Boundary: es wird ausschließlich vom
Sluice-Dispatch aufgerufen, *nachdem* `guarded_egress` released hat — es sieht nie
Rohtext und ist NIE direkt von Konsumenten erreichbar (Netz-Regel, §7.3; zusätzlich
optionales Shared Secret `SLUICE_GATEWAY_TOKEN`, Header `X-Sluice-Gateway-Token`).
Es hält nur den API-Key seines eigenen Providers.

Endpunkte (interner Vertrag Kern → Gateway, versioniert §7.4):
- `GET  /v1/health`    — Liveness, meldet den bedienten Provider.
- `POST /v1/complete`  — {messages, model, max_tokens[, stream]} →
                         JSON {text, model, provider} bzw. SSE `data: {"delta": …}` + `[DONE]`.

Start (systemd-Template `deploy/sluice-gateway@.service`, Ports ab 17890):
    SLUICE_PROVIDER=anthropic uvicorn --factory sluice.gateway:app --port 17890
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from sluice.providers import (
    ProviderAdapter,
    ProviderConfigError,
    ProviderError,
    canonical_provider,
    select_provider,
)

log = structlog.get_logger("sluice.gateway")


def create_gateway_app(
    provider: str | None = None,
    *,
    adapter_factory: Callable[[str], ProviderAdapter] = select_provider,
    token: str | None = None,
) -> Starlette:
    """App-Factory. Provider: Argument > Env `SLUICE_PROVIDER`; fehlt beides,
    schlägt der Start fehl — fail-closed, ein Gateway ohne Provider ergibt keinen Sinn.

    adapter_factory ist bewusst `select_provider` (direkte Adapter) — ein Gateway
    leitet nie an ein weiteres Gateway weiter. token (Fallback: Env
    `SLUICE_GATEWAY_TOKEN`): wenn gesetzt, muss jeder Request den Header
    `X-Sluice-Gateway-Token` mit exakt diesem Wert tragen, sonst 401.
    """
    raw = provider if provider is not None else os.environ.get("SLUICE_PROVIDER", "").strip()
    if not raw:
        raise ProviderConfigError(
            "Gateway ohne Provider: SLUICE_PROVIDER setzen (anthropic|openai|gemini|mistral)."
        )
    name = canonical_provider(raw)
    expected_token = (
        token if token is not None else os.environ.get("SLUICE_GATEWAY_TOKEN", "").strip() or None
    )
    log.info("gateway.start", provider=name, token_required=expected_token is not None)

    def _error(status: int, error_type: str, reason: str) -> JSONResponse:
        return JSONResponse({"error": {"type": error_type, "reason": reason}}, status_code=status)

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "provider": name})

    async def complete(request: Request) -> JSONResponse | StreamingResponse:
        if expected_token and request.headers.get("X-Sluice-Gateway-Token") != expected_token:
            return _error(401, "gateway_unauthorized", "Gültiger X-Sluice-Gateway-Token fehlt.")

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _error(400, "gateway_bad_request", "Body ist kein gültiges JSON.")

        messages = body.get("messages")
        model = body.get("model")
        if not isinstance(messages, list) or not messages or not model:
            return _error(400, "gateway_bad_request", "Pflichtfelder: messages (nicht leer) und model.")
        max_tokens = int(body.get("max_tokens", 1024))

        try:
            adapter = adapter_factory(name)
        except ProviderConfigError as exc:
            return _error(500, "gateway_config", str(exc))

        try:
            if body.get("stream"):
                chunks = adapter.stream(messages, model=model, max_tokens=max_tokens)

                async def sse() -> Any:
                    async for delta in chunks:
                        yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"

                return StreamingResponse(sse(), media_type="text/event-stream")

            response = await adapter.complete(messages, model=model, max_tokens=max_tokens)
            return JSONResponse(
                {"text": response.text, "model": response.model, "provider": response.provider}
            )
        except ProviderConfigError as exc:
            return _error(500, "gateway_config", str(exc))
        except ProviderError as exc:
            return _error(502, "gateway_upstream", str(exc))

    return Starlette(
        routes=[
            Route("/v1/health", health, methods=["GET"]),
            Route("/v1/complete", complete, methods=["POST"]),
        ]
    )


def app() -> Starlette:
    """Für `uvicorn --factory sluice.gateway:app` — Provider aus SLUICE_PROVIDER."""
    return create_gateway_app()
