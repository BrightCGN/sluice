"""Sluice NER-Dienst — eigenständiger Prozess, schmale Schnittstelle (Spec §7.5, Rev. 12).

Er tut **genau eins**: Text rein, Spans raus. Er kennt weder Profile noch Modi noch
Pseudonyme; er maskiert nichts. Diese Enge ist kein Minimalismus um seiner selbst willen,
sondern die Bedingung dafür, dass das Modell austauschbar bleibt und zwei Modelle
vergleichend laufen können, **ohne den Chokepoint zu duplizieren** (§7.5). Jede
Anonymisierungslogik, die hierher wandert, wäre ein zweiter Riegel neben Sluice.

Ausdrücklich **nicht** hier (Akzeptanzkriterium): Pseudonym-Zuordnung, Modus-Schalter,
Profilbindung, Maskierungslogik, Schwellwert-Anwendung. Alles davon bleibt in Sluice
(`sluice/ner/client.py`, `sluice/modes/pii_ner.py`).

Endpunkte (versioniert §7.4, unversionierte Aliase für den Vertrag aus der Anforderung):
- `GET  /v1/health`  (= `/health`) — Readiness; meldet erst `ok`, wenn das Modell geladen ist.
- `GET  /v1/info`    (= `/info`)   — Modellname, Revision, geladene Präzision, Backend,
                                     `score_floor`. Sluice übernimmt das in die
                                     Profilverankerung (§5.4).
- `POST /v1/detect`  (= `/detect`) — `{text, labels[, batch_size]}` → `{spans:[{start,
                                     end,label,score}]}`, Offsets als **Zeichen**-Offsets.

Wie die Provider-Gateways (Rev. 6/8) ist das ein **interner** Dienst hinter der Boundary:
nie direkt von Konsumenten erreichbar, eigener System-User `sluice-ner`, optionales
Shared Secret `SLUICE_NER_TOKEN` (Header `X-Sluice-Ner-Token`).

Start (systemd `deploy/sluice-ner.service`, Port 17900):
    SLUICE_NER_MODEL=fastino/gliner2-privacy-filter-PII-multi \
    uvicorn --factory sluice.ner.service:app --port 17900
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from sluice.ner import DEFAULT_LABELS, FIXED_BATCH_SIZE
from sluice.ner.engine import NerEngine, build_engine_from_env

log = structlog.get_logger("sluice.ner.service")

MAX_TEXT_CHARS = 100_000


def create_ner_app(
    engine: NerEngine | None = None,
    *,
    engine_factory: Callable[[], NerEngine] = build_engine_from_env,
    token: str | None = None,
) -> Starlette:
    """App-Factory. `engine` ist der Injektionspunkt für Tests (kein Modell, kein Netz).

    Das Modell wird **beim Start** geladen, nicht beim ersten Request: ein Dienst, der
    `/v1/health` bejaht und dann bei `/v1/detect` scheitert, würde Sluice erst mitten im
    Egress-Pfad fail-closed blockieren (§5.3). Lieber gar nicht erst hochkommen.
    """
    resolved_engine = engine if engine is not None else engine_factory()
    expected_token = (
        token if token is not None else os.environ.get("SLUICE_NER_TOKEN", "").strip() or None
    )
    info = resolved_engine.info()
    log.info(
        "ner.service.start",
        model=info.model,
        revision=info.revision or "(unpinned)",
        precision=info.precision,
        backend=info.backend,
        score_floor=info.score_floor,
        token_required=expected_token is not None,
    )

    def _error(status: int, error_type: str, reason: str) -> JSONResponse:
        return JSONResponse({"error": {"type": error_type, "reason": reason}}, status_code=status)

    def _authorized(request: Request) -> bool:
        return not expected_token or request.headers.get("X-Sluice-Ner-Token") == expected_token

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "model": info.model})

    async def service_info(_: Request) -> JSONResponse:
        """Die Modellidentität — Grundlage der Profilverankerung in Sluice (§5.4)."""
        return JSONResponse(
            {
                "model": info.model,
                "revision": info.revision,
                "precision": info.precision,
                "labels": list(info.labels or DEFAULT_LABELS),
                "backend": info.backend,
                "score_floor": info.score_floor,
                "batch_size": FIXED_BATCH_SIZE,
            }
        )

    async def detect(request: Request) -> JSONResponse:
        """Text rein, Spans raus — mehr nicht (§7.5)."""
        if not _authorized(request):
            return _error(401, "ner_unauthorized", "Gültiger X-Sluice-Ner-Token fehlt.")

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _error(400, "ner_bad_request", "Body ist kein gültiges JSON.")
        if not isinstance(body, dict):
            return _error(400, "ner_bad_request", "Body ist kein JSON-Objekt.")

        text = body.get("text")
        if not isinstance(text, str):
            return _error(400, "ner_bad_request", "Pflichtfeld: text (String).")
        if len(text) > MAX_TEXT_CHARS:
            return _error(
                413, "ner_too_large", f"Text überschreitet {MAX_TEXT_CHARS} Zeichen."
            )

        raw_labels = body.get("labels") or list(DEFAULT_LABELS)
        if not isinstance(raw_labels, list) or not all(isinstance(x, str) for x in raw_labels):
            return _error(400, "ner_bad_request", "labels muss eine Liste von Strings sein.")
        labels = tuple(raw_labels)

        # Kein dynamisches Batching (§5.4): eine abweichende Batchgröße würde die
        # Reduktionsreihenfolge ändern und Grenzfälle am Schwellwert kippen lassen.
        requested_batch = body.get("batch_size", FIXED_BATCH_SIZE)
        if requested_batch != FIXED_BATCH_SIZE:
            return _error(
                400,
                "ner_bad_request",
                f"batch_size ist auf {FIXED_BATCH_SIZE} fixiert (Determinismus, §5.4).",
            )

        try:
            spans = resolved_engine.detect(text, labels)
        except Exception as exc:  # noqa: BLE001 - der Dienst meldet, Sluice blockiert
            log.error("ner.detect.failed", error=str(exc))
            return _error(500, "ner_engine_error", f"Erkennung fehlgeschlagen: {exc}")

        return JSONResponse(
            {
                "spans": [
                    {"start": s.start, "end": s.end, "label": s.label, "score": s.score}
                    for s in spans
                ],
                "model": info.model,
                "revision": info.revision,
            }
        )

    def _routes() -> list[Route]:
        handlers: list[tuple[str, Any, str]] = [
            ("/health", health, "GET"),
            ("/info", service_info, "GET"),
            ("/detect", detect, "POST"),
        ]
        routes: list[Route] = []
        for path, handler, method in handlers:
            # Versionierter Pfad ist der Vertrag (§7.4); der unversionierte bleibt als
            # Alias bestehen, weil die Anforderung ihn wörtlich so nennt.
            routes.append(Route(f"/v1{path}", handler, methods=[method]))
            routes.append(Route(path, handler, methods=[method]))
        return routes

    return Starlette(routes=_routes())


def app() -> Starlette:
    """Für `uvicorn --factory sluice.ner.service:app` — Modell aus SLUICE_NER_MODEL."""
    return create_ner_app()
