"""Evaluations-Skript — Metrik-Logik (Spec §5.4, Rev. 12).

Das Eval-Skript ist die Grundlage, auf der der Schwellwert gesetzt wird. Wäre die
Metrik falsch, wäre der kalibrierte Wert falsch — und die Recall-Zusage der Boundary
damit unbelegt. Deshalb wird es hier selbst getestet, ohne Modell und ohne Netz.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from sluice.ner import NerSpan

# `scripts/` ist kein Paket — das Skript wird direkt geladen. Es muss dabei VOR
# `exec_module` in sys.modules stehen: `@dataclass` löst Typen über
# `sys.modules[cls.__module__]` auf und scheitert sonst an einem None-Eintrag.
_spec = importlib.util.spec_from_file_location(
    "eval_ner", Path(__file__).resolve().parent.parent / "scripts" / "eval_ner.py"
)
assert _spec and _spec.loader
eval_ner = importlib.util.module_from_spec(_spec)
sys.modules["eval_ner"] = eval_ner
_spec.loader.exec_module(eval_ner)

LABELS = ("person", "organization")
TEXT = "Richard Cochius arbeitet bei Acme GmbH"


class ScriptedEngine:
    """Liefert vorgegebene Spans — macht die Metrik exakt nachrechenbar."""

    def __init__(self, spans: list[NerSpan]) -> None:
        self._spans = spans

    def detect(self, text: str, labels: tuple[str, ...]) -> list[NerSpan]:
        return list(self._spans)


def document(*spans: tuple[int, int, str]) -> eval_ner.Document:
    return eval_ner.Document(
        text=TEXT, spans=tuple(eval_ner.GoldSpan(s, e, lbl) for s, e, lbl in spans)
    )


def test_perfekte_erkennung() -> None:
    engine = ScriptedEngine([NerSpan(0, 15, "person", 0.9)])
    report = eval_ner.evaluate(engine, [document((0, 15, "person"))], LABELS, 0.5, relaxed=False)
    assert report["per_label"]["person"] == {
        "tp": 1, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0, "f1": 1.0
    }


def test_uebersehener_span_zaehlt_als_fn() -> None:
    engine = ScriptedEngine([])
    report = eval_ner.evaluate(engine, [document((0, 15, "person"))], LABELS, 0.5, relaxed=False)
    assert report["per_label"]["person"]["fn"] == 1
    assert report["per_label"]["person"]["recall"] == 0.0


def test_schwellwert_filtert_vor_der_metrik() -> None:
    """Ein Span unter der Schwelle ist für die Metrik nicht vorhanden."""
    engine = ScriptedEngine([NerSpan(0, 15, "person", 0.2)])
    docs = [document((0, 15, "person"))]
    assert eval_ner.evaluate(engine, docs, LABELS, 0.1, relaxed=False)["micro"]["recall"] == 1.0
    assert eval_ner.evaluate(engine, docs, LABELS, 0.5, relaxed=False)["micro"]["recall"] == 0.0


def test_exakt_vs_relaxed() -> None:
    """Ein leicht verschobener Span verhindert das Leck trotzdem — beides wird gezeigt."""
    engine = ScriptedEngine([NerSpan(0, 14, "person", 0.9)])
    docs = [document((0, 15, "person"))]
    assert eval_ner.evaluate(engine, docs, LABELS, 0.5, relaxed=False)["micro"]["recall"] == 0.0
    assert eval_ner.evaluate(engine, docs, LABELS, 0.5, relaxed=True)["micro"]["recall"] == 1.0


def test_falscher_typ_ist_kein_treffer() -> None:
    engine = ScriptedEngine([NerSpan(0, 15, "organization", 0.9)])
    report = eval_ner.evaluate(engine, [document((0, 15, "person"))], LABELS, 0.5, relaxed=False)
    assert report["per_label"]["person"]["fn"] == 1
    assert report["per_label"]["organization"]["fp"] == 1


def test_metriken_sind_nach_typ_getrennt() -> None:
    """Der eigentliche Zweck: ein guter Durchschnitt darf einen schwachen Typ nicht verdecken."""
    engine = ScriptedEngine([NerSpan(0, 15, "person", 0.9)])
    docs = [document((0, 15, "person"), (29, 38, "organization"))]
    report = eval_ner.evaluate(engine, docs, LABELS, 0.5, relaxed=False)
    assert report["per_label"]["person"]["recall"] == 1.0
    assert report["per_label"]["organization"]["recall"] == 0.0
    assert report["worst_label"] == "organization"
    assert report["macro_recall"] == 0.5


def test_kalibrierung_waehlt_den_hoechsten_schwellwert_der_das_ziel_haelt() -> None:
    """Recall-Ziel statt F1-Optimum (§5.4) — und der schlechteste Typ entscheidet."""
    engine = ScriptedEngine(
        [NerSpan(0, 15, "person", 0.60), NerSpan(29, 38, "organization", 0.40)]
    )
    docs = [document((0, 15, "person"), (29, 38, "organization"))]
    result = eval_ner.calibrate(engine, docs, LABELS, 1.0, relaxed=False)
    # Über 0.40 fällt der Organisations-Span weg ⇒ höchster haltbarer Wert ist 0.40.
    assert result["chosen_threshold"] == 0.40
    assert result["warning"] is None


def test_unerreichbares_ziel_warnt_statt_still_zu_waehlen() -> None:
    """Kein stiller Rückfall auf einen F1-Wert — die Lücke muss sichtbar bleiben."""
    engine = ScriptedEngine([])
    docs = [document((0, 15, "person"))]
    result = eval_ner.calibrate(engine, docs, LABELS, 0.95, relaxed=False)
    assert result["chosen_threshold"] is None
    assert "KEIN Schwellwert" in result["warning"]
