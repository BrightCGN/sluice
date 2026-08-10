"""NER-Engines — austauschbare Backends hinter *einer* schmalen Schnittstelle (Spec §7.5, Rev. 12).

Der Sinn der Trennung: das Modell soll tauschbar sein und **zwei Modelle vergleichend
betrieben** werden können, ohne den Chokepoint zu duplizieren (§7.5). Deshalb kennt
Sluice nur `NerEngine.detect(text, labels) -> Spans` — welches Modell, welche Runtime
und welche Präzision dahinter liegt, meldet die Engine über `info()` und ist sonst
Implementierungsdetail.

**Keine Anonymisierungslogik hier** (Akzeptanzkriterium §7.5): keine Pseudonym-Zuordnung,
kein Modus-Schalter, keine Profilbindung, keine Maskierung. Nur Text rein, Spans raus.

**Determinismus (§5.4)** ist Aufgabe der Engine, nicht des Clients — der Cache dort macht
nur Wiederholungen billig. Über *Prozessneustarts* hinweg trägt die Zusage:
- fixierte Batchgröße (`FIXED_BATCH_SIZE`, immer 1 — kein dynamisches Batching),
- **ein** Intra-Op-Thread: mehr Threads ändern die Reduktionsreihenfolge in
  Gleitkommaoperationen, und genau daran kippen Grenzfälle am Schwellwert,
- fixierte numerische Präzision, die in `info()` ausgewiesen wird,
- kein Sampling, keine Zufallsquelle im Pfad (deshalb auch **kein generatives LLM**).

Die schweren Abhängigkeiten (`gliner`, `torch`, `onnxruntime`) werden **lazy** importiert:
`sluice.ner.service` bleibt ohne sie importierbar, damit Tests ohne Modell und ohne Netz
laufen (Test-Disziplin).
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

import structlog

from sluice.ner import DEFAULT_LABELS, NerSpan, ServiceInfo

log = structlog.get_logger("sluice.ner.engine")

# Untere Score-Grenze des Dienstes. Der Dienst filtert bewusst nur grob vor; die
# *wirksame* Schwelle ist die profilverankerte in Sluice (§5.4). Diese Grenze muss
# deshalb immer **unter** jedem Profil-Schwellwert liegen — der Client prüft das
# fail-closed gegen `ServiceInfo.score_floor`.
DEFAULT_SCORE_FLOOR = 0.05


@runtime_checkable
class NerEngine(Protocol):
    """Das Engine-Interface — bewusst zwei Methoden, mehr braucht der Vertrag nicht."""

    def info(self) -> ServiceInfo:
        """Modellidentität: Name, Revision, geladene Präzision, Backend (§7.5)."""
        ...

    def detect(self, text: str, labels: tuple[str, ...]) -> list[NerSpan]:
        """Zeichen-Offset-Spans. **Keine** Token-Offsets (§7.5)."""
        ...


def _score_floor() -> float:
    raw = os.environ.get("SLUICE_NER_SCORE_FLOOR", "").strip()
    if not raw:
        return DEFAULT_SCORE_FLOOR
    try:
        return float(raw)
    except ValueError:
        log.warning("ner.bad_score_floor", value=raw, fallback=DEFAULT_SCORE_FLOOR)
        return DEFAULT_SCORE_FLOOR


class GlinerEngine:
    """GLiNER über die Referenz-Bibliothek (PyTorch- oder ONNX-Runtime dahinter).

    GLiNER nimmt die Entitätstypen **zur Laufzeit** als Label-Liste entgegen — deshalb
    ist die Liste Konfiguration in Sluice und Teil der Anonymisierungs-Identität (§5.4),
    nicht im Modell eingebacken. Das ist auch der Grund, warum ein Modelltausch keinen
    Code-Eingriff braucht.

    `model_id` benennt Repo, `revision` den Commit-Hash — beide gehen unverändert in die
    Profilverankerung ein. Ohne festgenagelte `revision` ist die Identität wertlos: das
    Repo könnte sich unter derselben Kennung ändern.
    """

    def __init__(
        self,
        *,
        model_id: str,
        revision: str = "",
        precision: str = "fp32",
        device: str = "cpu",
        onnx: bool = False,
    ) -> None:
        self._model_id = model_id
        self._revision = revision
        self._precision = precision
        self._device = device
        self._onnx = onnx
        self._floor = _score_floor()
        self._model = self._load()

    def _load(self) -> object:
        try:
            from gliner import GLiNER  # lazy: nur im Dienst-Prozess nötig
        except ImportError as exc:  # pragma: no cover - Umgebungsfrage, nicht Logik
            raise RuntimeError(
                "GLiNER nicht installiert. Im NER-Dienst: pip install '.[ner]' "
                "(bzw. '.[ner-gpu]'). Der Sluice-Kern braucht das Paket NICHT."
            ) from exc

        kwargs: dict[str, object] = {}
        if self._revision:
            kwargs["revision"] = self._revision
        if self._onnx:
            # ONNX-Runtime-Pfad: der Ausweg, wenn PyTorch keine Pascal-Kernel (sm_61)
            # mitbringt — siehe scripts/probe_ner_hardware.py und docs/NER-SERVICE.md.
            kwargs["load_onnx_model"] = True
            kwargs["onnx_model_file"] = os.environ.get(
                "SLUICE_NER_ONNX_FILE", "onnx/model.onnx"
            )

        model = GLiNER.from_pretrained(self._model_id, **kwargs)
        self._pin_determinism()
        if self._device != "cpu":
            model = model.to(self._device)
        try:
            model.eval()
        except AttributeError:  # ONNX-Session hat kein eval()
            pass
        log.info(
            "ner.engine.loaded",
            model=self._model_id,
            revision=self._revision or "(unpinned)",
            precision=self._precision,
            device=self._device,
            onnx=self._onnx,
        )
        return model

    def _pin_determinism(self) -> None:
        """Fixiert alles, was die Span-Grenzen zwischen Läufen verschieben könnte (§5.4)."""
        try:
            import torch
        except ImportError:  # pragma: no cover - reiner ONNX-Betrieb
            return
        torch.manual_seed(0)
        torch.set_grad_enabled(False)
        # Ein Thread: Intra-Op-Parallelität ändert die Reduktionsreihenfolge und damit
        # die letzten Bits der Scores — genau die entscheiden am Schwellwert.
        torch.set_num_threads(1)
        try:
            torch.use_deterministic_algorithms(True)
        except (AttributeError, RuntimeError):  # pragma: no cover
            log.warning("ner.determinism.partial", hint="use_deterministic_algorithms nicht setzbar")

    def info(self) -> ServiceInfo:
        return ServiceInfo(
            model=self._model_id,
            revision=self._revision,
            precision=self._precision,
            labels=DEFAULT_LABELS,
            backend="gliner-onnx" if self._onnx else f"gliner-torch/{self._device}",
            score_floor=self._floor,
        )

    def detect(self, text: str, labels: tuple[str, ...]) -> list[NerSpan]:
        """Ein Text pro Aufruf — die fixierte Batchgröße ist Teil der Zusage (§5.4)."""
        entities = self._model.predict_entities(  # type: ignore[attr-defined]
            text, list(labels), threshold=self._floor
        )
        spans = [
            NerSpan(
                start=int(e["start"]),
                end=int(e["end"]),
                label=str(e["label"]),
                score=float(e.get("score", 0.0)),
            )
            for e in entities
        ]
        # Totale Ordnung schon hier — der Dienst liefert reproduzierbar sortiert.
        spans.sort(key=lambda s: (s.start, -s.end, s.label))
        return spans


def build_engine_from_env() -> NerEngine:
    """Baut die Engine aus der Dienst-Env. Fehlt das Modell, schlägt der Start fehl.

    Fail-closed beim Start statt beim ersten Request: ein NER-Dienst ohne Modell kann
    seine Aufgabe nie erfüllen, und ein Dienst, der `/v1/health` bejaht, aber bei
    `/v1/detect` scheitert, würde Sluice erst im Egress-Pfad blockieren.
    """
    model_id = os.environ.get("SLUICE_NER_MODEL", "").strip()
    if not model_id:
        raise RuntimeError(
            "NER-Dienst ohne Modell: SLUICE_NER_MODEL setzen (z. B. "
            "urchade/gliner_multi_pii-v1). Kandidaten und Auswahl: "
            "docs/NER-SERVICE.md."
        )
    return GlinerEngine(
        model_id=model_id,
        revision=os.environ.get("SLUICE_NER_REVISION", "").strip(),
        precision=os.environ.get("SLUICE_NER_PRECISION", "fp32").strip(),
        device=os.environ.get("SLUICE_NER_DEVICE", "cpu").strip(),
        onnx=os.environ.get("SLUICE_NER_ONNX", "").strip().lower() in ("1", "true", "yes"),
    )


__all__ = ["DEFAULT_SCORE_FLOOR", "GlinerEngine", "NerEngine", "build_engine_from_env"]
