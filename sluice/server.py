"""Sluice als eigenständiger HTTP-Service (Spec §1, §7 — Revision 3).

Der zentrale Service, den alle Projekte ansprechen, um mit externen KIs zu
kommunizieren. Versionierte Schnittstelle ab Tag 1 (§7.4): alle Pfade unter `/v1/`.

Endpunkte:
- `POST /v1/egress/guard`      — Guard-only (§7.1): sanitisieren + verifizieren,
                                 der Konsument dispatcht selbst.
- `POST /v1/chat/completions`  — Voll-Proxy (§7.2): Gate → Strategie → Verifier →
                                 Audit → Provider-Adapter → (reverse), inkl. SSE-Streaming.
- `GET  /v1/health`            — Liveness.

Modus (Rev. 9, safety first): Auslieferungs-Default ist **`strict`** (auto-redigierend,
Verifier fail-closed). Jeder registrierte Modus-Name (§3) ist per Profil (`mode`) oder pro
Request (`mode`, inkl. Legacy-Alias `reversible`/`irreversible`) wählbar; ein per Profil
gesperrter Modus (`allowed_modes`) ⇒ 403, ein unbekannter `mode` ⇒ 400 — beide fail-closed.

Start: `uvicorn sluice.server:app` mit `SLUICE_PROFILES=/pfad/profile.toml`.
Ohne Profil-Datei startet der Service mit leerer Profil-Menge — Default-Deny (§4.3):
jeder Request wird blockiert, nichts geht still raus.

Provider-Gateways (Rev. 6/7, §7.3): die Gateways sind EIGENSTÄNDIGE Services
(`sluice/gateway.py`, Ports ab 17890, jederzeit auf getrennte Server umziehbar).
Der Kern erreicht Provider AUSSCHLIESSLICH über sie (Rev. 7, verbindlich): pro
Provider muss `SLUICE_GATEWAY_<PROVIDER>_URL` gesetzt sein — fehlt sie, ist der
Aufruf ein Konfigurationsfehler (fail-closed), nie ein direkter Provider-Call.
Dispatch über das Gateway immer erst nach `released=true`.

Provider-Lock (Rev. 5, optional): ist `SLUICE_PROVIDER` gesetzt (bzw. `provider_lock`
übergeben), bedient DIESE Kern-Instanz genau einen Provider; Requests an andere ⇒ 403,
fail-closed. Guard-Kette und Verifier bleiben unverändert — der Lock ist eine
zusätzliche Schranke, nie eine Lockerung.
"""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Callable
from typing import Any

import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from sluice.audit import AuditLog
from sluice.dispatch import guarded_completion, guarded_stream
from sluice.guard import guarded_egress
from sluice.policy import Profile, load_profiles
from sluice.providers import (
    ProviderAdapter,
    ProviderConfigError,
    ProviderError,
    canonical_provider,
    select_egress_adapter,
)
from sluice.strategies import EgressPayload, Scope, is_registered_mode

log = structlog.get_logger("sluice.server")

# Legacy-Aliase für Request-`mode` (§7.2): Rev. 4 kannte nur reversible/irreversible.
# Rev. 9: jeder registrierte Modus-Name (§3) ist zulässig; alles andere ist 400 (fail-closed).
_MODE_ALIASES = {
    "reversible": "pseudonymizing",
    "irreversible": "generalizing",
}


def _bad_request(reason: str) -> JSONResponse:
    return JSONResponse({"error": {"type": "sluice_bad_request", "reason": reason}}, status_code=400)


def _blocked(reason: str) -> JSONResponse:
    return JSONResponse({"error": {"type": "sluice_blocked", "reason": reason}}, status_code=403)


def _effective_profile(profile: Profile, mode: str | None) -> Profile | JSONResponse:
    """Wendet den Request-`mode` an (§7.2, Rev. 9). Ohne `mode` gilt der Profil-Modus.

    Ein per Profil gesperrter Modus (`allowed_modes`, §4.1) wird nicht hier, sondern
    fail-closed im Guard abgewiesen — hier fällt nur ein unbekannter Modus-Name auf (400).
    """
    if mode is None:
        return profile
    resolved = _MODE_ALIASES.get(mode, mode)
    if not is_registered_mode(resolved):
        return _bad_request(f"Unbekannter mode '{mode}' (§3).")
    if resolved == profile.mode:
        return profile
    return dataclasses.replace(profile, mode=resolved)


def _raw_text(messages: list[dict[str, Any]]) -> str:
    """Alle Textinhalte — das reviewbare „Vorher" fürs Audit (§6)."""
    return "\n".join(
        m["content"] for m in messages if isinstance(m.get("content"), str)
    )


def create_app(
    profiles: dict[str, Profile] | None = None,
    *,
    profiles_path: str | None = None,
    adapter_factory: Callable[[str], ProviderAdapter] = select_egress_adapter,
    audit: AuditLog | None = None,
    provider_lock: str | None = None,
) -> Starlette:
    """App-Factory. Profil-Quelle: Argument > `profiles_path` > Env `SLUICE_PROFILES` > leer.

    Leer heißt Default-Deny (§4.3) — der Service startet, blockiert aber jeden Egress.
    adapter_factory/audit sind Injektionspunkte für Tests (kein Netz, §Test-Disziplin).
    provider_lock (Fallback: Env `SLUICE_PROVIDER`) beschränkt die Instanz auf genau
    einen Provider — Betriebsvariante „ein Service pro Gateway" (Rev. 5, §7.3).
    """
    if profiles is None:
        path = profiles_path or os.environ.get("SLUICE_PROFILES", "")
        if path:
            profiles = load_profiles(path)
        else:
            profiles = {}
            log.warning("server.no_profiles", hint="SLUICE_PROFILES nicht gesetzt → Default-Deny")

    resolved: dict[str, Profile] = profiles

    lock_raw = provider_lock if provider_lock is not None else os.environ.get(
        "SLUICE_PROVIDER", ""
    ).strip()
    lock: str | None = canonical_provider(lock_raw) if lock_raw else None
    if lock is not None:
        log.info("server.provider_lock", provider=lock)

    def _default_provider(effective: Profile | None) -> str:
        """Provider-Default (§7.2): auf einer Gateway-Instanz deren Lock, sonst der
        erste Allowlist-Eintrag. Bevorzugt den Allowlist-Namen, der kanonisch zum
        Lock passt, damit der Guard (§4.1) die Allowlist wörtlich prüfen kann."""
        allowlist = effective.provider_allowlist if effective else ()
        if lock is not None:
            for candidate in allowlist:
                if canonical_provider(candidate) == lock:
                    return candidate
            return lock  # nicht in der Allowlist — der Guard blockiert das fail-closed
        return allowlist[0] if allowlist else ""

    async def health(_: Request) -> JSONResponse:
        return JSONResponse(
            {"status": "ok", "profiles": len(resolved), "provider_lock": lock}
        )

    async def egress_guard(request: Request) -> JSONResponse:
        """Guard-only (§7.1): Antwort immer 200, `released` trägt die Entscheidung."""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _bad_request("Body ist kein gültiges JSON.")

        profile = resolved.get(body.get("profile", ""))
        outcome = await guarded_egress(
            profile=profile,
            purpose=body.get("purpose", ""),
            payload=EgressPayload(
                raw_text=body.get("raw_text", ""),
                generalized_text=body.get("generalized_text"),
                messages=body.get("messages"),
            ),
            scope=Scope(key=body["scope"]) if body.get("scope") else None,
            provider_target=body.get("provider"),
            audit=audit,
        )
        return JSONResponse(
            {
                "released": outcome.released,
                "sanitized_text": outcome.sanitized_text,
                "sanitized_messages": outcome.sanitized_messages,
                "reason": outcome.reason,
            }
        )

    async def chat_completions(request: Request) -> JSONResponse | StreamingResponse:
        """Voll-Proxy (§7.2): der eine zentrale Weg zu den externen KIs."""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _bad_request("Body ist kein gültiges JSON.")

        messages = body.get("messages")
        model = body.get("model")
        if not isinstance(messages, list) or not messages or not model:
            return _bad_request("Pflichtfelder: messages (nicht leer) und model.")

        # Profil aus dem Header; unbekannt/fehlend läuft als None in den Guard → Default-Deny.
        profile = resolved.get(request.headers.get("X-Sluice-Profile", ""))

        effective: Profile | None = None
        if profile is not None:
            result = _effective_profile(profile, body.get("mode"))
            if isinstance(result, JSONResponse):
                return result
            effective = result

        # Konsumenten müssen sich nicht kümmern (Rev. 3): purpose/provider defaulten auf
        # den ersten Profil-Eintrag; jede Wahl wird trotzdem im Guard geprüft (§4.1).
        purpose = body.get("purpose") or (
            effective.allowed_purposes[0] if effective and effective.allowed_purposes else ""
        )
        provider_target = body.get("provider") or _default_provider(effective)

        # Gateway-Instanz (Rev. 5): nur der eigene Provider, alles andere 403 fail-closed.
        if lock is not None and provider_target and canonical_provider(provider_target) != lock:
            return _blocked(
                f"Diese Instanz bedient nur Provider '{lock}' (SLUICE_PROVIDER); "
                f"angefragt: '{provider_target}'."
            )

        scope_key = request.headers.get("X-Sluice-Scope")
        scope = Scope(key=scope_key) if scope_key else None
        payload = EgressPayload(raw_text=_raw_text(messages), messages=messages)
        common: dict[str, Any] = {
            "profile": effective,
            "purpose": purpose,
            "payload": payload,
            "provider_target": provider_target,
            "model": model,
            "scope": scope,
            "max_tokens": int(body.get("max_tokens", 1024)),
            "audit": audit,
        }

        try:
            adapter = adapter_factory(provider_target) if provider_target else None
        except ProviderConfigError as exc:
            return JSONResponse(
                {"error": {"type": "sluice_provider_config", "reason": str(exc)}}, status_code=500
            )

        try:
            if body.get("stream"):
                stream_outcome = await guarded_stream(adapter=adapter, **common)
                if not stream_outcome.released or stream_outcome.chunks is None:
                    return _blocked(stream_outcome.reason)

                async def sse() -> Any:
                    async for chunk in stream_outcome.chunks:
                        event = {
                            "object": "chat.completion.chunk",
                            "model": model,
                            "choices": [{"index": 0, "delta": {"content": chunk}}],
                        }
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"

                return StreamingResponse(sse(), media_type="text/event-stream")

            outcome = await guarded_completion(adapter=adapter, **common)
            if not outcome.released:
                return _blocked(outcome.reason)
            return JSONResponse(
                {
                    "object": "chat.completion",
                    "model": outcome.model,
                    "provider": outcome.provider,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": outcome.response_text},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )
        except ProviderConfigError as exc:
            return JSONResponse(
                {"error": {"type": "sluice_provider_config", "reason": str(exc)}}, status_code=500
            )
        except ProviderError as exc:
            return JSONResponse(
                {"error": {"type": "sluice_provider_upstream", "reason": str(exc)}}, status_code=502
            )

    return Starlette(
        routes=[
            Route("/v1/health", health, methods=["GET"]),
            Route("/v1/egress/guard", egress_guard, methods=["POST"]),
            Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        ]
    )


# Für `uvicorn sluice.server:app` — Profile aus SLUICE_PROFILES, sonst Default-Deny.
app = create_app()
