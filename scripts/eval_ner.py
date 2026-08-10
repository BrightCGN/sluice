#!/usr/bin/env python3
"""NER-Evaluation und Schwellwert-Kalibrierung (Spec §5.4, Rev. 12).

Ohne Evaluationsdatensatz ist der Schwellwert nicht kalibrierbar — deshalb ist dieses
Skript die Voraussetzung dafür, den Wert im Profil überhaupt verantwortet setzen zu
können, und nicht bloß Beiwerk.

Was es tut:
- **Span-Level-Metriken, getrennt nach Entitätstyp.** Ein Modell mit gutem Durchschnitt
  kann bei einem einzelnen kritischen Typ versagen; ein Makro-F1 verdeckt genau das.
- **Recall pro Typ** wird immer ausgewiesen, auch wenn er schlecht ist.
- **Kalibrierung auf `--dev`, Bericht auf `--test`.** Auf demselben Split zu tunen und
  zu berichten überschätzt die Güte systematisch.
- **Recall-Ziel statt F1-Optimum.** Gesucht wird der *höchste* Schwellwert, der das
  Recall-Ziel noch hält — nicht das F1-Maximum. Übermaskierung ist ein Kostenfaktor,
  ein übersehener Span ein Datenleck.
- **Modellvergleich**: `--model` mehrfach angeben, um Kandidaten gegeneinander zu fahren.

Datenformat (JSONL, eine Zeile je Dokument):
    {"text": "...", "spans": [{"start": 0, "end": 15, "label": "person"}]}

Aufruf:
    python3 scripts/eval_ner.py --dev dev.jsonl --test test.jsonl \\
        --model urchade/gliner_multi_pii-v1 \\
        --model urchade/gliner_multi_pii-v1 \\
        --target-recall 0.98 --json eval-ergebnis.json

Ein Modellwechsel ist damit nachvollziehbar bewertbar: gleiche Splits, gleiches Skript,
vergleichbare Zahlen.
"""

from __future__ import annotations

# Wie probe_ner_hardware.py: als `python3 scripts/eval_ner.py` gestartet liegt `scripts/`
# auf sys.path, nicht das Repo-Root — `import sluice` schlüge sonst fehl.
import sys as _sys
from pathlib import Path as _Path

_REPO_ROOT = _Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_REPO_ROOT))

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CANDIDATE_THRESHOLDS = [round(0.05 * i, 2) for i in range(1, 20)]  # 0.05 … 0.95


@dataclass(frozen=True)
class GoldSpan:
    start: int
    end: int
    label: str


@dataclass(frozen=True)
class Document:
    text: str
    spans: tuple[GoldSpan, ...]


def load_dataset(path: Path) -> list[Document]:
    """Lädt JSONL. Fehlerhafte Zeilen sind ein harter Fehler — ein still übersprungenes
    Dokument würde den Recall künstlich beschönigen."""
    documents: list[Document] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            documents.append(
                Document(
                    text=raw["text"],
                    spans=tuple(
                        GoldSpan(int(s["start"]), int(s["end"]), str(s["label"]).lower())
                        for s in raw.get("spans", [])
                    ),
                )
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise SystemExit(f"{path}:{number} unlesbar: {exc}") from exc
    if not documents:
        raise SystemExit(f"{path} enthält keine Dokumente.")
    return documents


def spans_match(predicted: Any, gold: GoldSpan, *, relaxed: bool) -> bool:
    """Exakter Span-Match; `--relaxed` akzeptiert Überlappung bei gleichem Typ.

    Für eine Anonymisierungs-Boundary ist die relaxed-Sicht die betrieblich ehrlichere:
    ein Span, der den Namen überdeckt, aber eine Zeichenposition daneben endet, hat das
    Leck trotzdem verhindert. Ausgewiesen werden bewusst beide.
    """
    if predicted.label.lower() != gold.label:
        return False
    if relaxed:
        return predicted.start < gold.end and gold.start < predicted.end
    return predicted.start == gold.start and predicted.end == gold.end


def evaluate(
    engine: Any,
    documents: list[Document],
    labels: tuple[str, ...],
    threshold: float,
    *,
    relaxed: bool,
) -> dict[str, Any]:
    """Span-Level-Metriken, getrennt nach Entitätstyp (§5.4)."""
    per_label: dict[str, dict[str, int]] = {}

    def bucket(label: str) -> dict[str, int]:
        return per_label.setdefault(label, {"tp": 0, "fp": 0, "fn": 0})

    for document in documents:
        predicted = [s for s in engine.detect(document.text, labels) if s.score >= threshold]
        unmatched_gold = list(document.spans)
        matched_predictions: set[int] = set()

        for gold in list(unmatched_gold):
            hit = next(
                (
                    index
                    for index, p in enumerate(predicted)
                    if index not in matched_predictions and spans_match(p, gold, relaxed=relaxed)
                ),
                None,
            )
            if hit is not None:
                matched_predictions.add(hit)
                bucket(gold.label)["tp"] += 1
                unmatched_gold.remove(gold)

        for gold in unmatched_gold:
            bucket(gold.label)["fn"] += 1
        for index, prediction in enumerate(predicted):
            if index not in matched_predictions:
                bucket(prediction.label.lower())["fp"] += 1

    report: dict[str, Any] = {"threshold": threshold, "per_label": {}}
    total = {"tp": 0, "fp": 0, "fn": 0}
    for label, counts in sorted(per_label.items()):
        for key in total:
            total[key] += counts[key]
        report["per_label"][label] = _metrics(counts)
    report["micro"] = _metrics(total)
    # Makro-Recall über die Typen: verhindert, dass ein häufiger Typ einen seltenen,
    # aber kritischen überdeckt.
    recalls = [m["recall"] for m in report["per_label"].values()]
    report["macro_recall"] = round(sum(recalls) / len(recalls), 4) if recalls else 0.0
    report["worst_label"] = (
        min(report["per_label"].items(), key=lambda kv: kv[1]["recall"])[0]
        if report["per_label"]
        else None
    )
    return report


def _metrics(counts: dict[str, int]) -> dict[str, Any]:
    tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def calibrate(
    engine: Any,
    dev: list[Document],
    labels: tuple[str, ...],
    target_recall: float,
    *,
    relaxed: bool,
) -> dict[str, Any]:
    """Höchster Schwellwert, der das Recall-Ziel **auf jedem Typ** noch hält.

    Bewusst nicht das F1-Optimum (§5.4): F1 gewichtet einen zusätzlichen False Positive
    genauso wie einen übersehenen Span. Für eine Egress-Boundary stimmt diese Gewichtung
    nicht — Übermaskierung kostet Nutzen, ein übersehener Span kostet die Zusage.

    Das Kriterium ist der **schlechteste** Typ, nicht der Durchschnitt: ein Modell mit
    guter Makro-Zahl kann bei einem einzelnen kritischen Typ versagen.
    """
    trials: list[dict[str, Any]] = []
    for threshold in CANDIDATE_THRESHOLDS:
        report = evaluate(engine, dev, labels, threshold, relaxed=relaxed)
        worst_recall = min(
            (m["recall"] for m in report["per_label"].values()), default=0.0
        )
        trials.append(
            {
                "threshold": threshold,
                "worst_label_recall": round(worst_recall, 4),
                "macro_recall": report["macro_recall"],
                "micro_precision": report["micro"]["precision"],
                "micro_f1": report["micro"]["f1"],
                "meets_target": worst_recall >= target_recall,
            }
        )

    passing = [t for t in trials if t["meets_target"]]
    chosen = max(passing, key=lambda t: t["threshold"]) if passing else None
    return {
        "target_recall": target_recall,
        "trials": trials,
        "chosen_threshold": chosen["threshold"] if chosen else None,
        "warning": (
            None
            if chosen
            else "KEIN Schwellwert erreicht das Recall-Ziel auf allen Typen. Modell "
            "wechseln, Ziel begründet senken oder die Lücke per dictionary_terms "
            "schließen — NICHT stillschweigend einen F1-Wert übernehmen."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", required=True, type=Path, help="Dev-Split zum Kalibrieren")
    parser.add_argument("--test", type=Path, help="Test-Split zum Berichten (nie zum Tunen)")
    parser.add_argument("--model", action="append", required=True, help="mehrfach für Vergleich")
    parser.add_argument("--revision", action="append", default=[], help="je Modell, gleiche Reihenfolge")
    parser.add_argument("--labels", default="person,organization,address,location")
    parser.add_argument("--target-recall", type=float, default=0.98)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--onnx", action="store_true")
    parser.add_argument("--relaxed", action="store_true", help="Überlappung statt exaktem Match")
    parser.add_argument("--json", type=Path, help="Ergebnis als JSON ablegen")
    args = parser.parse_args()

    from sluice.ner.engine import GlinerEngine

    labels = tuple(x.strip() for x in args.labels.split(",") if x.strip())
    dev = load_dataset(args.dev)
    test = load_dataset(args.test) if args.test else None

    results: dict[str, Any] = {
        "labels": list(labels),
        "target_recall": args.target_recall,
        "matching": "relaxed" if args.relaxed else "exact",
        "dev_documents": len(dev),
        "test_documents": len(test) if test else 0,
        "models": {},
    }

    for index, model_id in enumerate(args.model):
        revision = args.revision[index] if index < len(args.revision) else ""
        print(f"\n=== {model_id} ({revision or 'unpinned'}) ===", file=sys.stderr)
        engine = GlinerEngine(
            model_id=model_id, revision=revision, device=args.device, onnx=args.onnx
        )

        calibration = calibrate(engine, dev, labels, args.target_recall, relaxed=args.relaxed)
        entry: dict[str, Any] = {"revision": revision, "calibration": calibration}
        if calibration["warning"]:
            print(f"  WARNUNG: {calibration['warning']}", file=sys.stderr)

        threshold = calibration["chosen_threshold"]
        if threshold is not None and test is not None:
            # Bericht auf dem Test-Split, mit dem auf DEV gewählten Schwellwert.
            entry["test"] = evaluate(engine, test, labels, threshold, relaxed=args.relaxed)
            print(f"  Schwellwert (dev): {threshold}", file=sys.stderr)
            for label, metrics in entry["test"]["per_label"].items():
                print(
                    f"    {label:16} recall={metrics['recall']:.3f} "
                    f"precision={metrics['precision']:.3f} (fn={metrics['fn']})",
                    file=sys.stderr,
                )
            print(f"    schlechtester Typ: {entry['test']['worst_label']}", file=sys.stderr)

        results["models"][model_id] = entry

    print(json.dumps(results, indent=2, ensure_ascii=False))
    if args.json:
        args.json.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n-> {args.json} geschrieben", file=sys.stderr)

    print(
        "\nDen gewählten Schwellwert ins Profil eintragen "
        "([profile.<name>.ner] threshold) — zusammen mit model_repo und model_revision. "
        "Erst dann ist die Anonymisierungs-Identität kalibriert (§5.4).",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
