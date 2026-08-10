"""Lange Texte: Zerlegung statt stiller Kürzung (Spec §5.3/§5.4, Rev. 12).

Das Fehlerbild, das diese Suite absichert, wurde am 2026-08-10 auf der Sluice-VM
beobachtet: GLiNER kürzt Eingaben über seinem Token-Fenster (dort 384) **still** auf
dieses Fenster. Bei 6.304 Zeichen fand das Modell den Namen im ersten Satz und den im
letzten nicht — ohne Fehler, ohne Warnung im Ergebnis, ohne dass Sluice etwas davon
mitbekam. Der Guard hätte `released=true` protokolliert für einen Text, dessen hinterer
Teil nie angesehen wurde.

Das ist schlimmer als ein Ausfall: ein Ausfall blockiert (§5.3), eine stille Kürzung
lässt durch. Deshalb zwei Ebenen, die hier beide geprüft werden:

1. **Netz darunter** — kürzt das Modell tatsächlich, blockiert der Dienst (413
   `ner_text_truncated` ⇒ `NerTruncationError` ⇒ `ModeUnavailableError` ⇒ 503).
2. **Regulärer Weg** — der Client zerlegt vorher in überlappende Stücke und rechnet die
   Offsets auf den Originaltext zurück, sodass Ebene 1 gar nicht erst greift.

Kein Netz, kein Modell: der Dienst wird über `httpx.MockTransport` gefahren und kürzt
dabei genauso wie das echte Modell.
"""

from __future__ import annotations

import json
import warnings
from typing import Any

import httpx
import pytest

from sluice.errors import ModeUnavailableError
from sluice.identity import NerIdentity
from sluice.ner import (
    NerConfig,
    NerIdentityError,
    NerSpan,
    NerTruncationError,
    ServiceInfo,
)
from sluice.ner.client import NerClient, split_chunks
from sluice.ner.engine import GlinerEngine
from sluice.ner.service import create_ner_app
from sluice.policy import parse_profiles

MODEL = "urchade/gliner_multi_pii-v1"
REVISION = "a1b2c3d4e5f6"

# Nachbau des VM-Befunds: ein Name ganz vorn, einer ganz hinten, dazwischen Fülltext.
VORN = "Anna Schmidt"
HINTEN = "Bernd Mueller"
FUELL = "Dies ist ein unauffaelliger Fuellsatz ohne Angaben. " * 120
TEXT = f"Am Anfang steht {VORN}. {FUELL} Ganz am Ende steht {HINTEN}."

# Das Fenster des simulierten Modells, in Zeichen gerechnet. Der echte Wert ist ein
# Token-Fenster; für den Test ist nur wichtig, dass es eine harte Grenze gibt.
FENSTER_ZEICHEN = 900


def ner_transport(
    *,
    fenster: int = FENSTER_ZEICHEN,
    gesehen: list[str] | None = None,
    max_tokens: int = 384,
) -> httpx.MockTransport:
    """Fake-NER-Dienst, der sich wie das echte Modell verhält — inklusive Kürzung.

    Findet die beiden Namen im *übergebenen* Text und liefert Spans relativ dazu. Ist der
    Text länger als das Fenster, antwortet er wie der echte Dienst mit 413: das ist der
    Unterschied zum echten Modell, das an dieser Stelle stumm bliebe — genau die Härtung,
    die geprüft wird.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/info"):
            return httpx.Response(
                200,
                json={
                    "model": MODEL,
                    "revision": REVISION,
                    "precision": "fp32",
                    "labels": ["person"],
                    "backend": "fake",
                    "score_floor": 0.05,
                    "max_tokens": max_tokens,
                },
            )
        if request.url.path.endswith("/detect"):
            text = json.loads(request.content)["text"]
            if gesehen is not None:
                gesehen.append(text)
            if len(text) > fenster:
                return httpx.Response(
                    413,
                    json={
                        "error": {
                            "type": "ner_text_truncated",
                            "reason": f"Sentence of length {len(text)} truncated to {fenster}",
                        }
                    },
                )
            spans: list[dict[str, Any]] = []
            for name in (VORN, HINTEN):
                start = text.find(name)
                if start >= 0:
                    spans.append(
                        {
                            "start": start,
                            "end": start + len(name),
                            "label": "person",
                            "score": 0.9,
                        }
                    )
            return httpx.Response(200, json={"spans": spans})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def make_client(
    *,
    max_chars: int = 700,
    overlap: int = 200,
    gesehen: list[str] | None = None,
    fenster: int = FENSTER_ZEICHEN,
    max_tokens: int = 384,
) -> NerClient:
    config = NerConfig(
        # Loopback: die Klartext-Schranke (§5.3) sperrt sonst zu Recht, weil über diesen
        # Hop unsanitisierter Rohtext geht.
        url="http://127.0.0.1:17900",
        threshold=0.3,
        labels=("person",),
        model_repo=MODEL,
        model_revision=REVISION,
        cache_size=0,
        max_chars_per_chunk=max_chars,
        chunk_overlap_chars=overlap,
    )
    transport = ner_transport(fenster=fenster, gesehen=gesehen, max_tokens=max_tokens)
    return NerClient(config, http_client=httpx.AsyncClient(transport=transport))


# ---- 1. split_chunks: die Zerlegung selbst -------------------------------------------


def test_kurzer_text_bleibt_ein_stueck() -> None:
    assert split_chunks("kurz", 700, 200) == ((0, "kurz"),)


def test_offsets_zeigen_auf_den_originaltext() -> None:
    """Die Rückrechnung hängt daran: `text[offset:offset+len(stueck)] == stueck`."""
    for offset, stueck in split_chunks(TEXT, 700, 200):
        assert TEXT[offset : offset + len(stueck)] == stueck


def test_zerlegung_deckt_den_gesamten_text_ab() -> None:
    """Kein Zeichen darf zwischen zwei Stücken verlorengehen — sonst bliebe es ungeprüft."""
    abgedeckt = set()
    for offset, stueck in split_chunks(TEXT, 700, 200):
        abgedeckt.update(range(offset, offset + len(stueck)))
    assert abgedeckt == set(range(len(TEXT)))


def test_stuecke_ueberlappen() -> None:
    """Ohne Überlappung würde eine Entität an der Schnittstelle in beiden Stücken verfehlt."""
    stuecke = split_chunks(TEXT, 700, 200)
    assert len(stuecke) > 1
    for (offset_a, stueck_a), (offset_b, _) in zip(stuecke, stuecke[1:]):
        assert offset_b < offset_a + len(stueck_a), "Stücke stoßen ohne Überlappung aneinander"


def test_uebergrosse_ueberlappung_terminiert() -> None:
    """Überlappung >= Stückgröße darf nicht zum Stillstand führen (Deckelung auf die Hälfte)."""
    stuecke = split_chunks(TEXT, 400, 10_000)
    assert len(stuecke) > 1
    assert stuecke[-1][0] + len(stuecke[-1][1]) == len(TEXT)


def test_stueckgroesse_null_ist_ein_fehler() -> None:
    with pytest.raises(Exception, match="max_chars_per_chunk"):
        split_chunks(TEXT, 0, 200)


# ---- 2. Der reguläre Weg: zerlegen, finden, zurückrechnen ----------------------------


async def test_name_am_textende_wird_gefunden() -> None:
    """Der VM-Befund als Test: ohne Zerlegung fehlte genau dieser Name."""
    client = make_client()
    spans = await client.detect(TEXT)

    gefunden = {TEXT[s.start : s.end] for s in spans}
    assert VORN in gefunden
    assert HINTEN in gefunden, "Name am Textende verfehlt — die Zerlegung greift nicht"


async def test_offsets_beziehen_sich_auf_den_originaltext() -> None:
    """Sluice redigiert im Originaltext; ein Stück-Offset würde die falsche Stelle treffen."""
    client = make_client()
    spans = await client.detect(TEXT)

    assert spans, "keine Spans"
    for span in spans:
        assert TEXT[span.start : span.end] in (VORN, HINTEN)


async def test_dubletten_aus_dem_ueberlappungsbereich_werden_zusammengefasst() -> None:
    """Eine Entität im Überlappungsbereich wird zweimal gefunden — sie darf einmal ankommen."""
    client = make_client()
    spans = await client.detect(TEXT)

    schluessel = [(s.start, s.end, s.label) for s in spans]
    assert len(schluessel) == len(set(schluessel))


async def test_zerlegung_ist_deterministisch() -> None:
    """Gleiche Eingabe, gleiche Parameter ⇒ gleiche Spans (§5.4) — auch ohne Cache."""
    erste = await make_client().detect(TEXT)
    zweite = await make_client().detect(TEXT)
    assert erste == zweite


async def test_kein_stueck_reisst_das_fenster() -> None:
    """Der Dienst darf im Normalbetrieb nie in die Kürzung laufen."""
    gesehen: list[str] = []
    await make_client(gesehen=gesehen).detect(TEXT)
    assert gesehen, "kein /detect-Aufruf"
    assert all(len(t) <= FENSTER_ZEICHEN for t in gesehen)


# ---- 3. Das Netz darunter: Kürzung blockiert -----------------------------------------


async def test_ohne_zerlegung_wird_blockiert_statt_gekuerzt() -> None:
    """Die entscheidende Zusage: lieber ein harter Fehler als eine stille Lücke (§5.3).

    `max_chars_per_chunk` absichtlich zu groß — der Dienst kürzt und meldet das. Ohne
    diese Härtung käme hier eine *erfolgreiche* Antwort mit unvollständigen Spans zurück,
    und der Guard würde den ungeprüften Rest freigeben.
    """
    client = make_client(max_chars=10_000, overlap=200, max_tokens=0)
    with pytest.raises(NerTruncationError):
        await client.detect(TEXT)


async def test_kuerzung_blockiert_generisch_ueber_den_mechanismus() -> None:
    """Der Guard greift an `ModeUnavailableError`, nicht am Namen `pii_ner` (§3)."""
    client = make_client(max_chars=10_000, overlap=200, max_tokens=0)
    with pytest.raises(ModeUnavailableError):
        await client.detect(TEXT)


async def test_stueckgroesse_groesser_als_fenster_blockiert_beim_ersten_kontakt() -> None:
    """Passt die Konfiguration nicht zum Modell, soll das sofort auffallen — nicht im Betrieb."""
    client = make_client(max_chars=5_000, overlap=200, max_tokens=384)
    with pytest.raises(NerIdentityError, match="Kontextfenster"):
        await client.detect(TEXT)


# ---- 4. Der Dienst meldet Kürzung als eigenen Fehlertyp -------------------------------


class TruncatingEngine:
    """Engine, die sich wie GLiNER über dem Fenster verhält — nur eben nicht stumm."""

    def info(self) -> ServiceInfo:
        return ServiceInfo(
            model=MODEL,
            revision=REVISION,
            precision="fp32",
            labels=("person",),
            backend="fake",
            score_floor=0.05,
            max_tokens=384,
        )

    def detect(self, text: str, labels: tuple[str, ...]) -> list[NerSpan]:
        if len(text) > FENSTER_ZEICHEN:
            raise NerTruncationError("Sentence of length 973 has been truncated to 384")
        return []


async def test_dienst_meldet_kuerzung_als_413_mit_eigenem_typ() -> None:
    """Nicht im 500er-Sammeltopf: der Aufrufer kann etwas dagegen tun (zerlegen)."""
    app = create_ner_app(TruncatingEngine())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ner.test"
    ) as http:
        response = await http.post("/v1/detect", json={"text": TEXT, "labels": ["person"]})
        assert response.status_code == 413
        assert response.json()["error"]["type"] == "ner_text_truncated"


async def test_info_meldet_das_kontextfenster() -> None:
    """Ohne gemeldetes Fenster kann Sluice die Stückgröße nicht gegenprüfen (§5.3)."""
    app = create_ner_app(TruncatingEngine())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ner.test"
    ) as http:
        assert (await http.get("/v1/info")).json()["max_tokens"] == 384


# ---- 4b. Die Engine erhebt die Kürzungs-Warnung zur Ausnahme --------------------------


class _StubModel:
    """Verhält sich wie GLiNER über dem Fenster: warnt und liefert trotzdem Spans."""

    def __init__(self, *, warnt: bool) -> None:
        self.warnt = warnt

    def predict_entities(self, text: str, labels: list[str], threshold: float) -> list[dict]:
        if self.warnt:
            warnings.warn(
                f"Sentence of length {len(text)} has been truncated to 384",
                UserWarning,
                stacklevel=2,
            )
        return [{"start": 0, "end": 5, "label": "person", "score": 0.9}]


def _engine(*, warnt: bool) -> GlinerEngine:
    """Engine ohne `__init__` — `gliner` ist im Test nicht installiert und nicht nötig."""
    engine = object.__new__(GlinerEngine)
    engine._model = _StubModel(warnt=warnt)  # type: ignore[attr-defined]
    engine._floor = 0.05  # type: ignore[attr-defined]
    engine._max_tokens = 384  # type: ignore[attr-defined]
    return engine


def test_engine_macht_aus_der_kuerzungs_warnung_einen_fehler() -> None:
    """GLiNER warnt nur — für einen Egress-Riegel muss daraus ein Abbruch werden (§5.3)."""
    with pytest.raises(NerTruncationError):
        _engine(warnt=True).detect("x" * 5_000, ("person",))


def test_kuerzung_wird_auch_beim_zweiten_aufruf_erkannt() -> None:
    """Pythons „einmal pro Fundstelle"-Registry darf die Warnung nicht verschlucken.

    Sonst griffe die Härtung ausgerechnet im Dauerbetrieb nicht mehr: der erste lange
    Text blockierte, jeder weitere liefe still gekürzt durch.
    """
    engine = _engine(warnt=True)
    for _ in range(3):
        with pytest.raises(NerTruncationError):
            engine.detect("x" * 5_000, ("person",))


def test_ohne_kuerzung_bleibt_die_erkennung_unberuehrt() -> None:
    spans = _engine(warnt=False).detect("kurzer Text", ("person",))
    assert [(s.start, s.end, s.label) for s in spans] == [(0, 5, "person")]


# ---- 5. Profil und Identität ----------------------------------------------------------


def test_profil_lehnt_zerlegung_ohne_ueberlappung_ab() -> None:
    """Überlappung 0 hieße: jede Entität an einer Schnittstelle wird verfehlt."""
    with pytest.raises(ValueError, match="chunk_overlap_chars"):
        parse_profiles(
            """
            [profile."x"]
            mode = "pii_ner"
            egress_enabled = true
            allowed_purposes = ["t"]
            provider_allowlist = ["anthropic"]
            detector_profile = "pii_de"
              [profile."x".ner]
              url = "http://127.0.0.1:17900"
              chunk_overlap_chars = 0
            """
        )


def test_profil_lehnt_ueberlappung_groesser_als_stueck_ab() -> None:
    with pytest.raises(ValueError, match="chunk_overlap_chars"):
        parse_profiles(
            """
            [profile."x"]
            mode = "pii_ner"
            egress_enabled = true
            allowed_purposes = ["t"]
            provider_allowlist = ["anthropic"]
            detector_profile = "pii_de"
              [profile."x".ner]
              url = "http://127.0.0.1:17900"
              max_chars_per_chunk = 300
              chunk_overlap_chars = 400
            """
        )


def test_zerlegung_steht_in_der_anonymisierungs_identitaet() -> None:
    """Andere Schnitte ⇒ andere Spans. Ohne die Werte wäre der Digest eine Falschaussage."""
    basis = NerConfig(model_repo=MODEL, max_chars_per_chunk=700, chunk_overlap_chars=200)
    anders = NerConfig(model_repo=MODEL, max_chars_per_chunk=400, chunk_overlap_chars=200)

    identitaet = NerIdentity.from_config(basis).as_dict()
    assert identitaet["max_chars_per_chunk"] == 700
    assert identitaet["chunk_overlap_chars"] == 200
    assert NerIdentity.from_config(anders).as_dict() != identitaet
