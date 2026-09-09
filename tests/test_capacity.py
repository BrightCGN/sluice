"""Kapazitäts-Telemetrie (Spec §7.6, Rev. 16).

Die eine Regel, an der die meisten Fälle hier hängen: **unbekannt ist `None`, nie `0`.**
Ein Provider, der keinen Kopfstand meldet, darf im Snapshot nicht aussehen wie einer mit
aufgebrauchtem Kontingent — sonst wäre die Auswahl (§4.5) auf einer Vermutung gebaut.

Kein Netz: der Gateway-Vertrag läuft in-process über `httpx.ASGITransport`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from sluice.audit import AuditLog
from sluice.capacity import (
    CapacityStore,
    RateLimit,
    StreamTelemetry,
    Usage,
    parse_reset,
    rate_limit_from_dict,
    rate_limit_from_headers,
    usage_from_payload,
)
from sluice.gateway import create_gateway_app
from sluice.policy import Profile
from sluice.providers import ProviderResponse
from sluice.providers.remote import RemoteGatewayAdapter
from sluice.server import create_app

# ---------- Header/Body-Lesen: nur melden, was gemessen wurde ----------


def test_a_provider_without_rate_limit_headers_yields_none_not_zeroes():
    assert rate_limit_from_headers({}, prefix="x") is None
    assert rate_limit_from_headers({"content-type": "application/json"}, prefix="anthropic") is None


def test_anthropic_and_openai_header_schemes_are_both_understood():
    anthropic = rate_limit_from_headers(
        {
            "anthropic-ratelimit-requests-limit": "1000",
            "anthropic-ratelimit-requests-remaining": "900",
            "anthropic-ratelimit-tokens-limit": "80000",
            "anthropic-ratelimit-tokens-remaining": "40000",
        },
        prefix="anthropic",
    )
    assert anthropic is not None
    assert anthropic.headroom() == pytest.approx(0.5)  # das knappste Kontingent bindet

    openai = rate_limit_from_headers(
        {
            "x-ratelimit-limit-requests": "500",
            "x-ratelimit-remaining-requests": "100",
            "x-ratelimit-reset-requests": "6m0s",
        },
        prefix="x",
    )
    assert openai is not None
    assert openai.headroom() == pytest.approx(0.2)
    assert openai.reset_seconds == pytest.approx(360.0)


def test_headroom_is_unknown_when_no_axis_is_measurable():
    assert RateLimit(tokens_remaining=500).headroom() is None  # Rest ohne Limit sagt nichts
    assert RateLimit().headroom() is None


def test_reset_durations_are_parsed_and_nonsense_stays_none():
    assert parse_reset("12") == pytest.approx(12.0)
    assert parse_reset("1.5s") == pytest.approx(1.5)
    assert parse_reset("120ms") == pytest.approx(0.12)
    assert parse_reset("2m30s") == pytest.approx(150.0)
    assert parse_reset(None) is None
    assert parse_reset("irgendwann") is None


def test_all_three_usage_dialects_are_understood():
    assert usage_from_payload({"usage": {"input_tokens": 3, "output_tokens": 4}}) == Usage(3, 4)
    assert usage_from_payload({"usage": {"prompt_tokens": 3, "completion_tokens": 4}}) == Usage(3, 4)
    assert usage_from_payload(
        {"usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4}}
    ) == Usage(3, 4)
    assert usage_from_payload({"text": "ohne usage"}) is None


def test_the_wire_form_round_trips_without_carrying_derived_values():
    original = RateLimit(tokens_limit=100, tokens_remaining=25)
    assert rate_limit_from_dict(original.as_dict()) == original
    assert rate_limit_from_dict({"headroom": 0.9}) is None  # abgeleitet allein trägt nichts


# ---------- Store: best-effort, aber nicht erfunden ----------


def test_usage_accumulates_while_the_rate_limit_is_the_newest_reading():
    store = CapacityStore()
    store.record(
        provider="anthropic",
        model="m",
        usage=Usage(10, 5),
        rate_limit=RateLimit(tokens_limit=100, tokens_remaining=90),
    )
    store.record(
        provider="anthropic",
        model="m",
        usage=Usage(1, 2),
        rate_limit=RateLimit(tokens_limit=100, tokens_remaining=70),
    )
    record = store.snapshot()[0]
    assert record.calls == 2
    assert record.usage == Usage(11, 7)
    assert store.headroom("anthropic") == pytest.approx(0.7)


def test_a_call_without_telemetry_still_counts_and_keeps_the_last_known_reading():
    """Dass gerufen wurde, ist selbst eine Auskunft — und die letzte belastbare
    Messung zu verwerfen, weil gerade keine kam, wäre Informationsverlust."""
    store = CapacityStore()
    store.record(
        provider="gemini",
        model="g",
        usage=Usage(1, 1),
        rate_limit=RateLimit(tokens_limit=10, tokens_remaining=5),
    )
    store.record(provider="gemini", model="g", usage=None, rate_limit=None)
    record = store.snapshot()[0]
    assert record.calls == 2
    assert store.headroom("gemini") == pytest.approx(0.5)


def test_a_provider_never_seen_has_no_headroom_rather_than_a_full_one():
    assert CapacityStore().headroom("mistral") is None


# ---------- Gateway-Vertrag (§7.4) ----------


class FakeAdapter:
    name = "fake"

    def __init__(
        self,
        *,
        usage: Usage | None = None,
        rate_limit: RateLimit | None = None,
        chunks: tuple[str, ...] = ("a", "b"),
    ) -> None:
        self.usage = usage
        self.rate_limit = rate_limit
        self.chunks = chunks

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int = 1024,
        tools: list[dict[str, Any]] | None = None,
    ) -> ProviderResponse:
        return ProviderResponse(
            text="ok",
            model=model,
            provider=self.name,
            usage=self.usage,
            rate_limit=self.rate_limit,
        )

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int = 1024,
        telemetry: StreamTelemetry | None = None,
    ) -> AsyncIterator[str]:
        for chunk in self.chunks:
            yield chunk
        if telemetry is not None:
            telemetry.usage = self.usage
            telemetry.rate_limit = self.rate_limit


def _gateway_client(adapter: FakeAdapter) -> httpx.AsyncClient:
    app = create_gateway_app("anthropic", adapter_factory=lambda name: adapter)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    )


@pytest.mark.anyio
async def test_gateway_passes_usage_and_rate_limit_through():
    adapter = FakeAdapter(
        usage=Usage(11, 22), rate_limit=RateLimit(tokens_limit=100, tokens_remaining=60)
    )
    async with _gateway_client(adapter) as client:
        resp = await client.post(
            "/v1/complete", json={"messages": [{"role": "user", "content": "x"}], "model": "m"}
        )
    data = resp.json()
    assert data["usage"] == {"input_tokens": 11, "output_tokens": 22, "total_tokens": 33}
    assert data["rate_limit"]["headroom"] == pytest.approx(0.6)


@pytest.mark.anyio
async def test_gateway_omits_the_fields_entirely_when_nothing_was_measured():
    """Rückwärtskompatibel UND ehrlich: keine Messung heißt kein Feld, nicht Null."""
    async with _gateway_client(FakeAdapter()) as client:
        resp = await client.post(
            "/v1/complete", json={"messages": [{"role": "user", "content": "x"}], "model": "m"}
        )
    data = resp.json()
    assert "usage" not in data and "rate_limit" not in data


@pytest.mark.anyio
async def test_gateway_stream_emits_a_terminal_telemetry_event_before_done():
    adapter = FakeAdapter(usage=Usage(5, 6), chunks=("Hallo ", "Welt"))
    async with _gateway_client(adapter) as client:
        resp = await client.post(
            "/v1/complete",
            json={"messages": [{"role": "user", "content": "x"}], "model": "m", "stream": True},
        )
        events = [
            json.loads(line[5:].strip())
            for line in resp.text.splitlines()
            if line.startswith("data:") and line[5:].strip() != "[DONE]"
        ]
    assert [e.get("delta") for e in events if "delta" in e] == ["Hallo ", "Welt"]
    assert events[-1]["usage"]["output_tokens"] == 6  # terminal, nach dem letzten Delta
    assert resp.text.rstrip().endswith("[DONE]")


@pytest.mark.anyio
async def test_the_core_reads_the_gateway_telemetry_back():
    adapter = FakeAdapter(
        usage=Usage(7, 8), rate_limit=RateLimit(requests_limit=10, requests_remaining=2)
    )
    app = create_gateway_app("anthropic", adapter_factory=lambda name: adapter)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as http:
        remote = RemoteGatewayAdapter(
            provider="anthropic", base_url="http://gateway", http_client=http
        )
        response = await remote.complete([{"role": "user", "content": "x"}], model="m")

        telemetry = StreamTelemetry()
        chunks = [
            c
            async for c in remote.stream(
                [{"role": "user", "content": "x"}], model="m", telemetry=telemetry
            )
        ]

    assert response.usage == Usage(7, 8)
    assert response.rate_limit is not None
    assert response.rate_limit.headroom() == pytest.approx(0.2)
    # Das terminale Event darf NICHT als Text durchgereicht werden.
    assert chunks == ["a", "b"]
    assert telemetry.usage == Usage(7, 8)


# ---------- Der lesende Endpunkt (§7.6) ----------

CRATE = Profile(
    name="crate",
    mode="strict",
    egress_enabled=True,
    allowed_purposes=("playlist_curation",),
    provider_allowlist=("anthropic",),
    detector_profile="media",
)


class CoreFake:
    name = "anthropic"

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int = 1024,
        tools: list[dict[str, Any]] | None = None,
    ) -> ProviderResponse:
        return ProviderResponse(
            text="ok",
            model=model,
            provider="anthropic",
            usage=Usage(100, 200),
            rate_limit=RateLimit(tokens_limit=1000, tokens_remaining=400),
        )

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int = 1024,
        telemetry: StreamTelemetry | None = None,
    ) -> AsyncIterator[str]:
        yield "ok"


@pytest.mark.anyio
async def test_capacity_endpoint_reports_what_the_dispatch_booked():
    store = CapacityStore()
    app = create_app(
        {"crate": CRATE},
        adapter_factory=lambda name: CoreFake(),
        audit=AuditLog(),
        capacity=store,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://sluice"
    ) as client:
        completion = await client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "90s Eurodance"}],
                "model": "claude-opus-5",
            },
            headers={"X-Sluice-Profile": "crate"},
        )
        report = await client.get("/v1/capacity")

    # Der Konsument sieht den Verbrauch direkt in seiner Antwort (Crate liest ihn
    # bereits, um eine leere Antwort von einer abgeschnittenen zu unterscheiden).
    assert completion.json()["usage"]["total_tokens"] == 300

    data = report.json()
    assert data["scope"] == "instance" and data["best_effort"] is True
    record = data["records"][0]
    assert record["provider"] == "anthropic"
    assert record["usage"]["total_tokens"] == 300
    assert record["rate_limit"]["headroom"] == pytest.approx(0.4)


@pytest.mark.anyio
async def test_a_blocked_egress_books_no_capacity():
    """Der Adapter wird nie berührt (Invariante 2) — also darf auch nichts gezählt werden."""
    store = CapacityStore()
    app = create_app(
        {"crate": CRATE},
        adapter_factory=lambda name: CoreFake(),
        audit=AuditLog(),
        capacity=store,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://sluice"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "x"}],
                "model": "claude-opus-5",
                "purpose": "nicht_erlaubt",
            },
            headers={"X-Sluice-Profile": "crate"},
        )
    assert resp.status_code == 403
    assert store.snapshot() == ()
