"""NER-Dienst unter Last: Loop frei, Erkennung serialisiert (Spec §5.4/§7.5, Rev. 12).

Die Erkennung ist CPU-gebunden und dauert auf CPU Sekunden (`NER-SERVICE.md` §3.1). Sie
synchron im async-Handler auszuführen — wie es bis 2026-08-11 der Fall war — hat zwei
Folgen, von denen die zweite im Betrieb teuer wird:

1. Parallele Erkennungen serialisieren. Das ist **gewollt** und bleibt so.
2. Der Event-Loop ist währenddessen belegt, also antwortet auch `/v1/health` nicht. Ein
   Health-Check schlägt damit ausgerechnet unter Last fehl und erklärt den Dienst für tot,
   während er arbeitet — und ein Neustart mitten in der Erkennung macht es schlimmer.

Die Auflösung ist nicht „mehr Nebenläufigkeit", sondern die Trennung beider Fragen: die
Erkennung wandert in den Threadpool (Loop frei), bleibt aber durch ein Lock serialisiert
(immer nur eine). Nebenläufige Aufrufe auf demselben Modell wären weder für GLiNER als
thread-sicher belegt noch mit der Determinismus-Zusage (§5.4) vereinbar.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from sluice.ner import NerSpan, ServiceInfo
from sluice.ner.service import create_ner_app

TEXT = "Anna Schmidt in Köln."


class LangsameEngine:
    """Engine, die spürbar rechnet — und mitschreibt, ob zwei Läufe sich überschneiden."""

    def __init__(self, dauer: float = 0.30) -> None:
        self.dauer = dauer
        self.laufend = 0
        self.max_gleichzeitig = 0
        self.fenster: list[tuple[float, float]] = []

    def info(self) -> ServiceInfo:
        return ServiceInfo(
            model="fake", revision="r1", precision="fp32",
            labels=("person",), backend="fake", score_floor=0.05, max_tokens=384,
        )

    def detect(self, text: str, labels: tuple[str, ...]) -> list[NerSpan]:
        start = time.perf_counter()
        self.laufend += 1
        self.max_gleichzeitig = max(self.max_gleichzeitig, self.laufend)
        time.sleep(self.dauer)  # blockierend — genau wie die echte Erkennung
        self.laufend -= 1
        self.fenster.append((start, time.perf_counter()))
        return [NerSpan(start=0, end=12, label="person", score=0.9)]


def client(engine: LangsameEngine) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_ner_app(engine)),
        base_url="http://ner.test",
    )


async def test_health_antwortet_waehrend_einer_erkennung() -> None:
    """Der eigentliche Fix: ein Health-Check darf unter Last nicht fehlschlagen.

    Gemessen wird ab **vor** dem Start der Erkennung, nicht danach. Der Unterschied ist
    nicht kosmetisch: blockiert die Erkennung den Loop, kommt auch die Zeitmessung selbst
    erst nach ihr wieder zum Zug — der Test würde dann eine schnelle Antwort messen und
    genau den Fehler übersehen, den er finden soll.
    """
    engine = LangsameEngine(dauer=0.40)
    async with client(engine) as http:
        begonnen = time.perf_counter()
        erkennung = asyncio.create_task(
            http.post("/v1/detect", json={"text": TEXT, "labels": ["person"]})
        )
        # Der Erkennung Gelegenheit geben, wirklich anzulaufen. Blockiert sie den Loop,
        # kehrt dieses `sleep` erst nach ihr zurück — und genau das macht die Messung
        # unterscheidungsfähig.
        await asyncio.sleep(0.05)

        gesundheit = await http.get("/v1/health")
        gebraucht = time.perf_counter() - begonnen

        assert gesundheit.status_code == 200
        assert gebraucht < 0.20, (
            f"/v1/health war erst nach {gebraucht:.2f}s da (Erkennung dauert 0,40 s) — "
            f"der Event-Loop war blockiert"
        )
        assert (await erkennung).status_code == 200


async def test_erkennungen_laufen_nacheinander() -> None:
    """Serialisierung bleibt: nebenläufige Läufe auf demselben Modell sind nicht belegt."""
    engine = LangsameEngine(dauer=0.20)
    async with client(engine) as http:
        antworten = await asyncio.gather(
            *[http.post("/v1/detect", json={"text": TEXT, "labels": ["person"]}) for _ in range(3)]
        )

    assert all(a.status_code == 200 for a in antworten)
    assert engine.max_gleichzeitig == 1, (
        f"{engine.max_gleichzeitig} Erkennungen gleichzeitig — Serialisierung verletzt"
    )
    # Zusätzlich über die Zeitfenster: kein Fenster darf ein anderes überlappen.
    for a, b in zip(sorted(engine.fenster), sorted(engine.fenster)[1:]):
        assert a[1] <= b[0] + 1e-6, f"Läufe überlappen: {a} und {b}"


async def test_alle_anfragen_werden_beantwortet() -> None:
    """Serialisieren heißt anstehen, nicht verwerfen."""
    engine = LangsameEngine(dauer=0.05)
    async with client(engine) as http:
        antworten = await asyncio.gather(
            *[http.post("/v1/detect", json={"text": TEXT, "labels": ["person"]}) for _ in range(5)]
        )
    assert [a.status_code for a in antworten] == [200] * 5
    assert all(a.json()["spans"] for a in antworten)
