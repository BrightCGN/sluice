"""`pii_ner` über die HTTP-Schnittstelle (Spec §5.3/§5.4/§7, Rev. 12).

Prüft die Akzeptanzkriterien dort, wo der Konsument sie tatsächlich sieht: am Vertrag.
Der Modus wird über den **dokumentierten Registry-Erweiterungspunkt** (§3) mit einem
gemockten NER-Client hinterlegt — kein Netz, kein Modell.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from sluice.audit import AuditLog
from sluice.modes import pii_ner as pii_ner_module
from sluice.modes import register_mode
from sluice.modes.pii_ner import PiiNerMode
from sluice.ner import NerConfig
from sluice.ner.client import NerClient
from sluice.policy import Profile
from sluice.providers import ProviderResponse
from sluice.server import create_app
from tests.test_pii_ner import MODEL, REVISION, ner_transport

TEXT = "Max Mustermann, IBAN DE89 3704 0044 0532 0130 00"

NER_CONFIG = NerConfig(
    url="https://ner.invalid",
    threshold=0.30,
    model_repo=MODEL,
    model_revision=REVISION,
    model_precision="fp32",
    threshold_declared=True,
)

PROFILE = Profile(
    name="prismclaw-ner",
    mode="pii_ner",
    egress_enabled=True,
    allowed_purposes=("chat",),
    provider_allowlist=("anthropic",),
    detector_profile="pii_de",
    ner=NER_CONFIG,
)


class FakeAdapter:
    name = "anthropic"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    async def complete(
        self, messages: list[dict[str, Any]], *, model: str, max_tokens: int = 1024
    ) -> ProviderResponse:
        self.calls.append(messages)
        return ProviderResponse(text="ok", model=model, provider=self.name)

    async def stream(self, messages: Any, *, model: str, max_tokens: int = 1024) -> Any:
        yield "ok"


@pytest.fixture
def use_mocked_ner() -> Iterator[Any]:
    """Hinterlegt `pii_ner` über den Registry-Erweiterungspunkt (§3) mit Mock-Transport.

    Räumt danach auf: Registry-Eintrag und Instanz-Cache werden zurückgesetzt, damit
    andere Tests den echten Modus sehen.
    """
    state: dict[str, Any] = {"transport_kwargs": {}}

    def factory(profile: Profile) -> PiiNerMode:
        transport = ner_transport(
            [{"start": 0, "end": 15, "label": "person", "score": 0.94}],
            **state["transport_kwargs"],
        )
        config = profile.ner or NER_CONFIG
        return PiiNerMode(
            detector_profile=profile.detector_profile,
            ner_config=config,
            client=NerClient(config, http_client=httpx.AsyncClient(transport=transport)),
        )

    from sluice.modes import _instances, _MODE_FACTORIES

    original = _MODE_FACTORIES.get("pii_ner")
    register_mode("pii_ner", factory)
    _instances.clear()
    try:
        yield state
    finally:
        if original is not None:
            register_mode("pii_ner", original)
        _instances.clear()


def client(**kwargs: Any) -> httpx.AsyncClient:
    app = create_app({PROFILE.name: PROFILE}, audit=AuditLog(level="metadata"), **kwargs)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://sluice.test"
    )


async def test_beide_stufen_wirken_im_proxy_pfad(use_mocked_ner: Any) -> None:
    adapter = FakeAdapter()
    async with client(adapter_factory=lambda _: adapter) as http:
        response = await http.post(
            "/v1/chat/completions",
            headers={"X-Sluice-Profile": "prismclaw-ner"},
            json={"messages": [{"role": "user", "content": TEXT}], "model": "claude-x"},
        )
    assert response.status_code == 200
    # Was beim Provider ankommt, trägt weder Namen noch IBAN.
    sent = adapter.calls[0][0]["content"]
    assert "Max Mustermann" not in sent
    assert "DE89" not in sent
    assert "[PERSON]" in sent and "[IBAN]" in sent


async def test_ner_ausfall_ist_503_und_nicht_403(use_mocked_ner: Any) -> None:
    """Der eindeutige Fehler beim Aufrufer (§5.3) — Ausfall ist kein Policy-Nein."""
    use_mocked_ner["transport_kwargs"] = {"fail": httpx.ConnectError("weg")}
    adapter = FakeAdapter()
    async with client(adapter_factory=lambda _: adapter) as http:
        response = await http.post(
            "/v1/chat/completions",
            headers={"X-Sluice-Profile": "prismclaw-ner"},
            json={"messages": [{"role": "user", "content": TEXT}], "model": "claude-x"},
        )
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "sluice_mode_unavailable"
    # Und der Provider wurde nie berührt — kein Teilergebnis geht raus.
    assert adapter.calls == []


async def test_policy_ablehnung_bleibt_403(use_mocked_ner: Any) -> None:
    """Zur Abgrenzung: der andere Fall darf sich nicht mitverändert haben."""
    async with client(adapter_factory=lambda _: FakeAdapter()) as http:
        response = await http.post(
            "/v1/chat/completions",
            headers={"X-Sluice-Profile": "unbekannt"},
            json={"messages": [{"role": "user", "content": TEXT}], "model": "claude-x"},
        )
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "sluice_blocked"


async def test_guard_endpunkt_meldet_ausfall_als_error_type(use_mocked_ner: Any) -> None:
    """Der Guard-only-Pfad antwortet vertragsgemäß 200 — die Art steht im Body (§7.1)."""
    use_mocked_ner["transport_kwargs"] = {"fail": httpx.ReadTimeout("zu langsam")}
    async with client() as http:
        response = await http.post(
            "/v1/egress/guard",
            json={"profile": "prismclaw-ner", "purpose": "chat", "raw_text": TEXT},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["released"] is False
    assert body["error_type"] == "mode_unavailable"
    assert body["sanitized_text"] is None


async def test_identitaets_endpunkt_liefert_modell_und_schwellwert() -> None:
    """Akzeptanzkriterium: beides ist aus der Identität ableitbar (§5.4)."""
    async with client() as http:
        response = await http.get("/v1/anonymization-identity?profile=prismclaw-ner")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "pii_ner"
    assert body["ner"]["model_repo"] == MODEL
    assert body["ner"]["model_revision"] == REVISION
    assert body["ner"]["threshold"] == 0.30
    assert len(body["digest"]) == 64


async def test_identitaets_endpunkt_erfindet_keine_identitaet() -> None:
    async with client() as http:
        response = await http.get("/v1/anonymization-identity?profile=gibtsnicht")
    assert response.status_code == 404
