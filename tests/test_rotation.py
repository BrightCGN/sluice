"""Modell-Rotation (Spec §4.5, Rev. 16) — die Wahl, die Sluice treffen darf, und die,
die es nie treffen darf.

Zwei Zusagen tragen diese Datei:

1. **Ein genanntes Modell wird nie ersetzt.** Rotation gibt es nur auf ausdrückliche
   Anfrage (`model: "auto"`) und nur aus einer im Profil deklarierten Menge.
2. **Die Menge kann die Allowlist nicht verlassen** — schon beim Laden, nicht erst im
   Egress-Pfad.

Kein Netz; der Provider-Aufruf läuft gegen einen Fake-Adapter.
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from sluice.audit import AuditLog
from sluice.capacity import CapacityStore, RateLimit, StreamTelemetry, Usage
from sluice.policy import Profile, parse_profiles
from sluice.providers import ProviderError, ProviderResponse
from sluice.rotation import (
    RotationConfig,
    RotationEntry,
    RotationError,
    effective_max_tokens,
    select_rotation,
)
from sluice.server import create_app

CRATE_TOML = """
[profile."crate"]
mode                = "strict"
egress_enabled      = true
allowed_purposes    = ["playlist_curation"]
provider_allowlist  = ["anthropic", "gemini", "openai"]
detector_profile    = ["media", "pii_de"]

  [profile."crate".rotation]
  policy = "random"

  [[profile."crate".rotation.models]]
  provider          = "anthropic"
  model             = "claude-opus-5"
  min_output_tokens = 16000

  [[profile."crate".rotation.models]]
  provider = "gemini"
  model    = "gemini-2.5-pro"

  [[profile."crate".rotation.models]]
  provider = "openai"
  model    = "gpt-5"
"""


class FakeAdapter:
    """Merkt sich, WOMIT er gerufen wurde — die Rotation ist genau daran ablesbar."""

    name = "fake"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, int]] = []  # (model, max_tokens)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int = 1024,
        tools: list[dict[str, Any]] | None = None,
    ) -> ProviderResponse:
        self.calls.append((model, max_tokens))
        if self.fail:
            raise ProviderError("upstream kaputt")
        # Der Provider meldet den präziseren Namen zurück — genau das, was der
        # Konsument als „tatsächlich benutzt" speichert (CRATE-SLUICE §12.1).
        return ProviderResponse(
            text="ok",
            model=f"{model}-20260101",
            provider=self.name,
            usage=Usage(input_tokens=10, output_tokens=20),
        )

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int = 1024,
        telemetry: StreamTelemetry | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append((model, max_tokens))
        yield "ok"


def _client(
    profiles: dict[str, Profile],
    *,
    adapter: FakeAdapter | None = None,
    audit: AuditLog | None = None,
    seed: int = 0,
    capacity: CapacityStore | None = None,
) -> tuple[httpx.AsyncClient, FakeAdapter, AuditLog]:
    adapter = adapter or FakeAdapter()
    audit = audit or AuditLog(level="metadata")
    app = create_app(
        profiles,
        adapter_factory=lambda name: adapter,
        audit=audit,
        capacity=capacity or CapacityStore(),
        # Zustandslos heißt nicht unprüfbar: ein injizierter RNG macht dieselbe
        # Wahl reproduzierbar, ohne dass der Betrieb dafür Zustand hielte.
        rng=random.Random(seed),
    )
    transport = httpx.ASGITransport(app=app)
    return (
        httpx.AsyncClient(transport=transport, base_url="http://sluice"),
        adapter,
        audit,
    )


def _profiles() -> dict[str, Profile]:
    return parse_profiles(CRATE_TOML)


BODY = {
    "messages": [{"role": "user", "content": "90s Eurodance für lange Autofahrt"}],
    "purpose": "playlist_curation",
}
HEADERS = {"X-Sluice-Profile": "crate"}


# ---------- Ladeprüfung: die Menge IST die Allowlist (§4.1/§4.5) ----------


def test_rotation_entry_outside_the_allowlist_is_a_load_error():
    """Ein Provider, der nicht in der Allowlist steht, darf nicht über die Rotation
    hereinkommen — sonst wäre die Rotation ein zweiter Weg, ein Ziel zu benennen."""
    toml = CRATE_TOML.replace('provider = "gemini"', 'provider = "mistral"')
    with pytest.raises(ValueError, match="provider_allowlist"):
        parse_profiles(toml)


def test_declared_but_empty_rotation_is_a_load_error():
    """„Ich rotiere" und dann passiert nichts wäre die stillste Form von „aus"."""
    with pytest.raises(ValueError, match="rotation.models"):
        parse_profiles(
            '[profile."x"]\nprovider_allowlist = ["anthropic"]\n'
            '  [profile."x".rotation]\n  policy = "random"\n'
        )


def test_entry_without_model_is_a_load_error():
    """Crate ließ solche Einträge still fallen; hier startet der Dienst nicht."""
    with pytest.raises(ValueError, match="provider UND model"):
        parse_profiles(
            '[profile."x"]\nprovider_allowlist = ["anthropic"]\n'
            '  [[profile."x".rotation.models]]\n  provider = "anthropic"\n'
        )


def test_duplicate_entry_is_a_load_error():
    """Ein Duplikat wäre unter `random` eine unsichtbare Gewichtung."""
    with pytest.raises(ValueError, match="doppelt"):
        parse_profiles(
            '[profile."x"]\nprovider_allowlist = ["anthropic"]\n'
            '  [[profile."x".rotation.models]]\n  provider = "anthropic"\n  model = "m"\n'
            '  [[profile."x".rotation.models]]\n  provider = "anthropic"\n  model = "m"\n'
        )


def test_unknown_policy_is_a_load_error():
    with pytest.raises(ValueError, match="rotation.policy"):
        parse_profiles(
            '[profile."x"]\nprovider_allowlist = ["anthropic"]\n'
            '  [profile."x".rotation]\n  policy = "least-recently-used"\n'
            '  [[profile."x".rotation.models]]\n  provider = "anthropic"\n  model = "m"\n'
        )


def test_profile_without_rotation_block_parses_to_an_empty_set():
    profile = parse_profiles('[profile."x"]\nprovider_allowlist = ["anthropic"]\n')["x"]
    assert not profile.rotation
    assert profile.rotation.entries == ()


# ---------- Auswahl (zustandslos) ----------


def test_random_policy_eventually_reaches_every_declared_model():
    """Zustandslos heißt: keine Garantie je Lauf, aber über viele Läufe alle."""
    config = _profiles()["crate"].rotation
    rng = random.Random(1234)
    seen = {select_rotation(config, rng=rng).model for _ in range(200)}
    assert seen == {"claude-opus-5", "gemini-2.5-pro", "gpt-5"}


def test_requested_provider_narrows_the_set_and_never_falls_back():
    config = _profiles()["crate"].rotation
    choice = select_rotation(config, provider="gemini", rng=random.Random(0))
    assert (choice.provider, choice.model) == ("gemini", "gemini-2.5-pro")

    # Ein Provider ohne Eintrag wird abgewiesen — NICHT auf einen anderen umgelenkt.
    with pytest.raises(RotationError, match="fail-closed statt Ausweichen"):
        select_rotation(config, provider="mistral", rng=random.Random(0))


def test_provider_is_matched_canonically_so_claude_finds_anthropic():
    """Profile schreiben historisch `claude`, der Lock (Rev. 5) sagt `anthropic`."""
    config = _profiles()["crate"].rotation
    assert select_rotation(config, provider="claude", rng=random.Random(0)).model == (
        "claude-opus-5"
    )


def test_empty_rotation_raises_instead_of_inventing_a_model():
    with pytest.raises(RotationError, match="Rotationsmenge"):
        select_rotation(RotationConfig(), rng=random.Random(0))


def test_headroom_policy_prefers_the_provider_with_the_most_room():
    capacity = CapacityStore()
    capacity.record(
        provider="anthropic",
        model="claude-opus-5",
        usage=None,
        rate_limit=RateLimit(tokens_limit=100, tokens_remaining=5),
    )
    capacity.record(
        provider="gemini",
        model="gemini-2.5-pro",
        usage=None,
        rate_limit=RateLimit(tokens_limit=100, tokens_remaining=90),
    )
    config = RotationConfig(
        policy="headroom",
        entries=(
            RotationEntry("anthropic", "claude-opus-5"),
            RotationEntry("gemini", "gemini-2.5-pro"),
            RotationEntry("openai", "gpt-5"),  # ohne Telemetrie ⇒ nimmt nicht teil
        ),
    )
    choice = select_rotation(config, capacity=capacity, rng=random.Random(0))
    assert choice.provider == "gemini"
    assert "Headroom" in choice.reason


def test_headroom_without_any_telemetry_falls_back_to_chance_not_to_a_fixed_first():
    """Kein Kapazitätsstand ist keine Bevorzugung — sonst gewänne dauerhaft der
    Eintrag, der zufällig oben steht."""
    config = RotationConfig(
        policy="headroom",
        entries=(
            RotationEntry("anthropic", "a"),
            RotationEntry("gemini", "b"),
            RotationEntry("openai", "c"),
        ),
    )
    rng = random.Random(7)
    seen = {
        select_rotation(config, capacity=CapacityStore(), rng=rng).model for _ in range(100)
    }
    assert seen == {"a", "b", "c"}


# ---------- Token-Budget: anheben, nie senken (§4.5 / CRATE-SLUICE §12.2) ----------


def test_min_output_tokens_raises_the_budget_but_never_lowers_it():
    entry = RotationEntry("anthropic", "claude-opus-5", min_output_tokens=16000)
    assert effective_max_tokens(1024, entry) == 16000
    assert effective_max_tokens(32000, entry) == 32000  # der Konsument weiß es besser
    assert effective_max_tokens(1024, RotationEntry("gemini", "g")) == 1024


# ---------- Endpoint (§7.2) ----------


@pytest.mark.anyio
async def test_auto_rotates_and_the_response_names_the_model_that_answered():
    client, adapter, _ = _client(_profiles(), seed=3)
    async with client:
        resp = await client.post(
            "/v1/chat/completions", json={**BODY, "model": "auto"}, headers=HEADERS
        )
    assert resp.status_code == 200
    called_model, called_budget = adapter.calls[0]
    assert called_model in {"claude-opus-5", "gemini-2.5-pro", "gpt-5"}
    # §12.1: die Antwort trägt, WAS geantwortet hat — nicht „auto", nicht das
    # Angefragte. Ohne das kann ein Konsument seine Bewertung je Modell nicht führen.
    assert resp.json()["model"] == f"{called_model}-20260101"
    assert resp.json()["model"] != "auto"


@pytest.mark.anyio
async def test_auto_raises_max_tokens_to_the_entry_floor():
    """Wählt Sluice ein Reasoning-Modell, muss es auch dessen Budget verantworten."""
    client, adapter, _ = _client(_profiles(), seed=3)
    async with client:
        await client.post(
            "/v1/chat/completions",
            json={**BODY, "model": "auto", "provider": "anthropic", "max_tokens": 1024},
            headers=HEADERS,
        )
    assert adapter.calls[0] == ("claude-opus-5", 16000)


@pytest.mark.anyio
async def test_an_explicitly_named_model_is_never_silently_replaced():
    """Der Kern der Rev.-16-Zusage: rotiert wird NUR auf Anfrage."""
    client, adapter, _ = _client(_profiles(), seed=3)
    async with client:
        resp = await client.post(
            "/v1/chat/completions",
            json={**BODY, "model": "gemini-2.5-flash", "provider": "gemini"},
            headers=HEADERS,
        )
    assert resp.status_code == 200
    assert adapter.calls[0][0] == "gemini-2.5-flash"


@pytest.mark.anyio
async def test_auto_without_a_declared_rotation_is_blocked_not_defaulted():
    profiles = parse_profiles(
        '[profile."temper"]\negress_enabled = true\n'
        'allowed_purposes = ["external_escalation"]\nprovider_allowlist = ["anthropic"]\n'
    )
    client, adapter, _ = _client(profiles)
    async with client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "hallo"}],
                "model": "auto",
                "purpose": "external_escalation",
            },
            headers={"X-Sluice-Profile": "temper"},
        )
    assert resp.status_code == 403
    assert "Rotationsmenge" in resp.json()["error"]["reason"]
    assert adapter.calls == []  # kein Provider berührt


@pytest.mark.anyio
async def test_auto_without_a_known_profile_is_default_deny():
    client, adapter, _ = _client(_profiles())
    async with client:
        resp = await client.post(
            "/v1/chat/completions",
            json={**BODY, "model": "auto"},
            headers={"X-Sluice-Profile": "gibt-es-nicht"},
        )
    assert resp.status_code == 403
    assert adapter.calls == []


@pytest.mark.anyio
async def test_auto_with_a_provider_outside_the_rotation_set_is_blocked():
    client, adapter, _ = _client(_profiles())
    async with client:
        resp = await client.post(
            "/v1/chat/completions",
            json={**BODY, "model": "auto", "provider": "mistral"},
            headers=HEADERS,
        )
    assert resp.status_code == 403
    assert adapter.calls == []


@pytest.mark.anyio
async def test_the_audit_entry_records_why_this_target_was_chosen():
    """Sobald Sluice das Ziel wählt, ist es nicht mehr aus dem Profil ableitbar —
    dann muss die Begründung im Log stehen, sonst ist der Eintrag unvollständig."""
    audit = AuditLog(level="metadata")
    client, _, _ = _client(_profiles(), audit=audit, seed=3)
    async with client:
        await client.post(
            "/v1/chat/completions", json={**BODY, "model": "auto"}, headers=HEADERS
        )
    entry = audit.entries[-1]
    assert entry.released is True
    assert entry.provider_selection is not None
    assert "policy=random" in entry.provider_selection
    assert entry.provider_target in {"anthropic", "gemini", "openai"}


@pytest.mark.anyio
async def test_a_consumer_named_target_leaves_the_selection_reason_empty():
    """None heißt hier: nicht Sluice hat gewählt. Das ist eine Auskunft, kein Fehlen."""
    audit = AuditLog(level="metadata")
    client, _, _ = _client(_profiles(), audit=audit)
    async with client:
        await client.post(
            "/v1/chat/completions",
            json={**BODY, "model": "gpt-5", "provider": "openai"},
            headers=HEADERS,
        )
    assert audit.entries[-1].provider_selection is None


@pytest.mark.anyio
async def test_a_failing_provider_is_reported_never_retried_on_another():
    """Kein stilles Ausweichen (§4.5/§10): sonst versteckt man genau die Information,
    die die Rotation sammeln soll — welches Modell unzuverlässig ist."""
    client, adapter, _ = _client(_profiles(), adapter=FakeAdapter(fail=True), seed=3)
    async with client:
        resp = await client.post(
            "/v1/chat/completions", json={**BODY, "model": "auto"}, headers=HEADERS
        )
    assert resp.status_code == 502
    assert len(adapter.calls) == 1  # genau ein Versuch


@pytest.mark.anyio
async def test_a_locked_gateway_instance_rotates_only_within_its_own_provider():
    """Sonst wählte die Rotation ein Ziel, das die Instanz gleich darauf 403t."""
    adapter = FakeAdapter()
    app = create_app(
        _profiles(),
        adapter_factory=lambda name: adapter,
        audit=AuditLog(),
        provider_lock="anthropic",
        capacity=CapacityStore(),
        rng=random.Random(0),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sluice") as client:
        for _ in range(10):
            resp = await client.post(
                "/v1/chat/completions", json={**BODY, "model": "auto"}, headers=HEADERS
            )
            assert resp.status_code == 200
    assert {model for model, _ in adapter.calls} == {"claude-opus-5"}


@pytest.mark.anyio
async def test_streaming_chunks_carry_the_resolved_model_not_auto():
    """Sonst stünde in jedem SSE-Chunk „auto" — eine Auskunft, die niemand brauchen kann."""
    client, _, _ = _client(_profiles(), seed=3)
    async with client:
        resp = await client.post(
            "/v1/chat/completions",
            json={**BODY, "model": "auto", "stream": True},
            headers=HEADERS,
        )
        body = resp.text
    assert '"model": "auto"' not in body
