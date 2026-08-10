#!/usr/bin/env python3
"""Hardware-Sondierung für den NER-Dienst — **vor** der Architekturentscheidung.

Die Regel lautet: erst messen, dann entscheiden. Dieses Skript ermittelt genau die
zwei Werte, an denen die Entscheidung hängt, und schreibt sie maschinenlesbar heraus,
damit sie in `docs/NER-SERVICE.md` nachvollziehbar landen.

1. **Enthält PyTorch auf dieser Maschine Pascal-Kernel (`sm_61`)?**
   Fehlt `sm_61` in `torch.cuda.get_arch_list()`, ist der PyTorch-CUDA-Pfad auf einer
   GTX 1080 Ti nicht verfügbar. Ausweg: ONNX Runtime mit CUDA-Provider.
2. **Reicht CPU-Inferenz?** Latenz für repräsentative Eingabelängen (200/1000/4000
   Zeichen). Reicht sie, ist CPU vorzuziehen — ein Chokepoint ohne GPU-Abhängigkeit ist
   betrieblich robuster. Auf einer CPU **ohne AVX2** (FX-6300) fallen die Kernel allerdings
   auf langsamere Pfade zurück; die Messung entscheidet, nicht die Vermutung.

Aufruf **auf der Zielmaschine**:
    python3 scripts/probe_ner_hardware.py                    # nur Umgebung
    python3 scripts/probe_ner_hardware.py --model <repo>     # inkl. Latenzmessung
    python3 scripts/probe_ner_hardware.py --model <repo> --json probe.json

Ohne `--model` läuft nur die Umgebungssondierung — die braucht weder Modell noch Netz.
"""

from __future__ import annotations

# Das Skript wird als `python3 scripts/probe_ner_hardware.py` von einer frischen Maschine
# aus gestartet — dann liegt `scripts/` auf sys.path, nicht das Repo-Root, und `import
# sluice` schlägt fehl. Repo-Root deshalb selbst ergänzen, statt PYTHONPATH zu verlangen.
import sys as _sys
from pathlib import Path as _Path

_REPO_ROOT = _Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_REPO_ROOT))

import argparse
import json
import platform
import statistics
import sys
import time
from typing import Any

# Repräsentative Eingabelängen aus der Anforderung.
SAMPLE_LENGTHS = (200, 1000, 4000)
WARMUP_RUNS = 2
MEASURED_RUNS = 5

# Deutscher Fülltext mit PII-Fläche — realistischer als Lorem ipsum, weil die
# Entitätsdichte die Latenz beeinflusst.
_SEED_TEXT = (
    "Sehr geehrte Frau Dr. Annegret Baumgartner, bezüglich Ihres Schreibens vom 3. März "
    "teilen wir Ihnen mit, dass die Überweisung an die Nordwind Logistik GmbH in Bremen "
    "über das Konto DE89 3704 0044 0532 0130 00 ausgeführt wurde. Für Rückfragen erreichen "
    "Sie Herrn Kowalski unter 0221 4710815 oder per Mail an t.kowalski@nordwind-logistik.de. "
    "Die Sachbearbeitung erfolgt durch das Büro Hamburg, Rothenbaumchaussee 12. "
)


def sample_text(length: int) -> str:
    """Text exakt `length` Zeichen lang — die Messgröße ist die Eingabelänge."""
    repeats = (length // len(_SEED_TEXT)) + 1
    return (_SEED_TEXT * repeats)[:length]


def probe_environment() -> dict[str, Any]:
    """Wert 1: Pascal-Kernel vorhanden? Plus alles, was die Entscheidung sonst stützt."""
    result: dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "processor": platform.processor(),
    }

    try:
        with open("/proc/cpuinfo", encoding="utf-8") as handle:
            cpuinfo = handle.read()
        for line in cpuinfo.splitlines():
            if line.startswith("model name"):
                result["cpu_model"] = line.split(":", 1)[1].strip()
                break
        flags = next(
            (ln.split(":", 1)[1].split() for ln in cpuinfo.splitlines() if ln.startswith("flags")),
            [],
        )
        # AVX2 fehlt auf dem FX-6300 — relevant, weil ONNX-Runtime- und
        # PyTorch-CPU-Kernel dann auf langsamere Pfade zurückfallen.
        result["avx"] = "avx" in flags
        result["avx2"] = "avx2" in flags
        result["avx512"] = any(f.startswith("avx512") for f in flags)
    except OSError:
        result["cpu_model"] = "unbekannt"

    try:
        import torch

        result["torch"] = torch.__version__
        result["torch_cuda_available"] = bool(torch.cuda.is_available())
        arch_list = list(torch.cuda.get_arch_list())
        result["torch_arch_list"] = arch_list
        # Ein CPU-only-Wheel (`+cpu`) hat GAR KEINE CUDA-Kernel — die Arch-Liste ist leer.
        # Das ist etwas anderes als ein CUDA-Build, dem sm_61 fehlt, und muss unterschieden
        # werden: sonst liest sich „sm_61 fehlt" wie eine Aussage über die GPU, obwohl nur
        # das falsche Wheel installiert ist.
        result["torch_cpu_only_build"] = "+cpu" in torch.__version__ or not arch_list
        # DIE Kernfrage für die GTX 1080 Ti (Compute Capability 6.1) — nur bei CUDA-Build
        # überhaupt beantwortbar.
        result["pascal_sm_61"] = ("sm_61" in arch_list) if arch_list else None
        if torch.cuda.is_available():
            result["cuda_device"] = torch.cuda.get_device_name(0)
            result["cuda_capability"] = list(torch.cuda.get_device_capability(0))
            free, total = torch.cuda.mem_get_info()
            result["cuda_vram_free_gb"] = round(free / 1024**3, 2)
            result["cuda_vram_total_gb"] = round(total / 1024**3, 2)
    except ImportError:
        result["torch"] = None
        result["pascal_sm_61"] = None

    try:
        import onnxruntime

        result["onnxruntime"] = onnxruntime.__version__
        result["onnx_providers"] = list(onnxruntime.get_available_providers())
        result["onnx_cuda_provider"] = "CUDAExecutionProvider" in result["onnx_providers"]
    except ImportError:
        result["onnxruntime"] = None

    return result


def probe_latency(model_id: str, *, revision: str, device: str, onnx: bool) -> dict[str, Any]:
    """Wert 2: reicht CPU-Inferenz? Latenz je Eingabelänge, Median und p95."""
    from sluice.ner import DEFAULT_LABELS
    from sluice.ner.engine import GlinerEngine

    load_start = time.perf_counter()
    engine = GlinerEngine(
        model_id=model_id, revision=revision, device=device, onnx=onnx, precision="fp32"
    )
    load_seconds = time.perf_counter() - load_start

    measurements: dict[str, Any] = {
        "model": model_id,
        "revision": revision or "(unpinned)",
        "device": device,
        "onnx": onnx,
        # ACHTUNG: enthält bei kaltem HF-Cache den Modell-Download. Für die Startzeit
        # des Dienstes ist nur ein Lauf mit warmem Cache aussagekräftig.
        "load_seconds": round(load_seconds, 2),
        "load_includes_download": None,  # vom Aufrufer zu setzen, wenn bekannt
        "by_length": {},
    }

    for length in SAMPLE_LENGTHS:
        text = sample_text(length)
        for _ in range(WARMUP_RUNS):  # Warmlauf: der erste Aufruf misst Lazy-Init mit
            engine.detect(text, DEFAULT_LABELS)

        timings: list[float] = []
        for _ in range(MEASURED_RUNS):
            start = time.perf_counter()
            spans = engine.detect(text, DEFAULT_LABELS)
            timings.append((time.perf_counter() - start) * 1000)

        measurements["by_length"][str(length)] = {
            "median_ms": round(statistics.median(timings), 1),
            "min_ms": round(min(timings), 1),
            "max_ms": round(max(timings), 1),
            "spans_found": len(spans),
        }

    return measurements


def recommend(environment: dict[str, Any], latency: dict[str, Any] | None) -> list[str]:
    """Leitet die Empfehlung aus den Messwerten ab — ohne sie vorwegzunehmen."""
    notes: list[str] = []

    if environment.get("torch") is None:
        notes.append("PyTorch nicht installiert — Wert 1 unbeantwortet, GPU-Pfad ungeprüft.")
    elif environment.get("torch_cpu_only_build"):
        notes.append(
            "PyTorch ist ein CPU-only-Build (kein CUDA einkompiliert) — Wert 1 damit NICHT "
            "beantwortet. Das sagt nichts über die GPU aus. Für die Antwort ein CUDA-Wheel "
            "installieren und erneut messen."
        )
    elif environment.get("pascal_sm_61") is False:
        notes.append(
            "sm_61 FEHLT in torch.cuda.get_arch_list() ⇒ PyTorch-CUDA-Pfad auf der "
            "GTX 1080 Ti nicht verfügbar. Bei GPU-Bedarf: ONNX Runtime + CUDA-Provider."
        )
    elif environment.get("pascal_sm_61"):
        notes.append("sm_61 vorhanden ⇒ PyTorch-CUDA-Pfad grundsätzlich nutzbar.")

    if environment.get("avx2") is False:
        notes.append(
            "Keine AVX2-Unterstützung — CPU-Kernel laufen auf langsameren Pfaden; "
            "die gemessene Latenz ist damit die realistische Untergrenze."
        )

    if latency is not None:
        worst = max(v["median_ms"] for v in latency["by_length"].values())
        notes.append(f"Schlechtester Median über alle Längen: {worst} ms.")
        short = latency["by_length"].get("200", {}).get("median_ms")
        if short is not None:
            notes.append(f"Median bei 200 Zeichen (Kurztext): {short} ms.")
        if worst < 500:
            notes.append(
                "CPU reicht ⇒ CPU wählen. Ein Chokepoint ohne GPU-Abhängigkeit ist "
                "betrieblich robuster als eine mit."
            )
        else:
            notes.append(
                "CPU-Latenz grenzwertig für den synchronen Egress-Pfad ⇒ GPU prüfen. "
                "VRAM sollte reichen: GLiNER (~205M) braucht grob ~0,8 GB in fp32."
            )
    else:
        notes.append("Keine Latenzmessung gelaufen (--model fehlt) ⇒ Wert 2 unbeantwortet.")

    return notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="HF-Repo des NER-Modells; ohne das nur Umgebungssondierung")
    parser.add_argument("--revision", default="", help="Commit-Hash — gehört in die Identität")
    parser.add_argument("--device", default="cpu", help="cpu | cuda")
    parser.add_argument("--onnx", action="store_true", help="ONNX-Runtime statt PyTorch")
    parser.add_argument("--json", help="Ergebnis zusätzlich als JSON hierhin schreiben")
    args = parser.parse_args()

    report: dict[str, Any] = {"environment": probe_environment(), "latency": None}
    if args.model:
        report["latency"] = probe_latency(
            args.model, revision=args.revision, device=args.device, onnx=args.onnx
        )
    report["notes"] = recommend(report["environment"], report["latency"])

    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
        print(f"\n-> {args.json} geschrieben", file=sys.stderr)

    print("\n=== Bewertung ===", file=sys.stderr)
    for note in report["notes"]:
        print(f"  - {note}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
