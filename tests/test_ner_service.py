"""NER-Dienst — schmale Schnittstelle, keine Anonymisierungslogik (Spec §7.5, Rev. 12).

ASGI in-process, kein Modell, kein Netz: die Engine wird injiziert (Test-Disziplin).
Das wichtigste Kriterium hier ist ein *negatives* — der Dienst darf nichts maskieren,
nichts pseudonymisieren, keine Profile kennen. Nur so bleibt das Modell austauschbar
und der Chokepoint einfach (§7.5).
"""

from __future__ import annotations

import httpx

from sluice.ner import NerSpan, ServiceInfo
from sluice.ner.service import create_ner_app

TEXT = "Richard Cochius wohnt in Köln und arbeitet bei Acme GmbH."


class FakeEngine:
    """Deterministische Engine ohne Modell — liefert feste Spans."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def info(self) -> ServiceInfo:
        return ServiceInfo(
            model="urchade/gliner_multi_pii-v1",
            revision="a1b2c3d4e5f6",
            precision="fp32",
            labels=("person", "organization", "address", "location"),
            backend="fake",
            score_floor=0.05,
        )

    def detect(self, text: str, labels: tuple[str, ...]) -> list[NerSpan]:
        self.calls.append((text, labels))
        return [
            NerSpan(start=0, end=15, label="person", score=0.93),
            NerSpan(start=47, end=56, label="organization", score=0.71),
        ]


def client(engine: FakeEngine | None = None, **kwargs: object) -> httpx.AsyncClient:
    app = create_ner_app(engine if engine is not None else FakeEngine(), **kwargs)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ner.test"
    )


async def test_health_meldet_bereitschaft() -> None:
    async with client() as http:
        for path in ("/v1/health", "/health"):
            response = await http.get(path)
            assert response.status_code == 200
            assert response.json()["status"] == "ok"


async def test_info_liefert_modellidentitaet() -> None:
    """Modellname, Revision und geladene Präzision — Grundlage der Profilverankerung (§5.4)."""
    async with client() as http:
        for path in ("/v1/info", "/info"):
            body = (await http.get(path)).json()
            assert body["model"] == "urchade/gliner_multi_pii-v1"
            assert body["revision"] == "a1b2c3d4e5f6"
            assert body["precision"] == "fp32"
            assert body["batch_size"] == 1


async def test_detect_liefert_zeichen_offsets() -> None:
    async with client() as http:
        response = await http.post(
            "/v1/detect", json={"text": TEXT, "labels": ["person", "organization"]}
        )
        assert response.status_code == 200
        spans = response.json()["spans"]
        assert spans[0] == {"start": 0, "end": 15, "label": "person", "score": 0.93}
        # Zeichen-Offsets, keine Token-Offsets (§7.5): sie müssen in den Originaltext passen.
        assert TEXT[spans[0]["start"] : spans[0]["end"]] == "Richard Cochius"


async def test_dienst_enthaelt_keine_anonymisierungslogik() -> None:
    """Akzeptanzkriterium: Text rein, Spans raus — mehr nicht (§7.5).

    Der Dienst gibt **keinen** maskierten Text zurück, kennt keine Platzhalter, keine
    Pseudonyme und kein Profil. Jede dieser Antwortformen wäre ein zweiter Riegel neben
    Sluice und würde den Chokepoint duplizieren.
    """
    async with client() as http:
        body = (await http.post("/v1/detect", json={"text": TEXT})).json()

    assert set(body) <= {"spans", "model", "revision"}
    assert "text" not in body and "sanitized" not in body and "masked" not in body
    serialized = str(body)
    for placeholder in ("[PERSON]", "[ORGANISATION]", "[IBAN]", "[NAME]"):
        assert placeholder not in serialized
    # Der Originaltext taucht nirgends maskiert oder ersetzt wieder auf.
    assert "Richard" not in serialized


async def test_labels_kommen_zur_laufzeit_vom_aufrufer() -> None:
    """GLiNER nimmt die Typen zur Laufzeit entgegen — deshalb gehören sie in die
    Sluice-Konfiguration und nicht in den Dienst (§5.4)."""
    engine = FakeEngine()
    async with client(engine) as http:
        await http.post("/v1/detect", json={"text": TEXT, "labels": ["person", "iban"]})
    assert engine.calls[-1][1] == ("person", "iban")


async def test_abweichende_batchgroesse_wird_abgelehnt() -> None:
    """Kein dynamisches Batching (§5.4) — die Ablehnung ist die Durchsetzung."""
    async with client() as http:
        response = await http.post("/v1/detect", json={"text": TEXT, "batch_size": 8})
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "ner_bad_request"


async def test_fehlender_text_ist_ein_bad_request() -> None:
    async with client() as http:
        assert (await http.post("/v1/detect", json={})).status_code == 400


async def test_token_schuetzt_den_internen_dienst() -> None:
    """Wie die Provider-Gateways ist der NER-Dienst intern (Rev. 6/8)."""
    async with client(token="geheim") as http:
        assert (await http.post("/v1/detect", json={"text": TEXT})).status_code == 401
        ok = await http.post(
            "/v1/detect", json={"text": TEXT}, headers={"X-Sluice-Ner-Token": "geheim"}
        )
        assert ok.status_code == 200


async def test_engine_fehler_wird_als_fehler_gemeldet_nicht_als_leere_menge() -> None:
    """Eine leere Span-Liste bei Engine-Fehler wäre stille Degradation (§5.3)."""

    class BrokenEngine(FakeEngine):
        def detect(self, text: str, labels: tuple[str, ...]) -> list[NerSpan]:
            raise RuntimeError("Modell weg")

    async with client(BrokenEngine()) as http:
        response = await http.post("/v1/detect", json={"text": TEXT})
    assert response.status_code == 500
    assert response.json()["error"]["type"] == "ner_engine_error"
