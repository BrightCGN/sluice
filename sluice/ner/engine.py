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
import warnings
from typing import Protocol, runtime_checkable

import structlog

from sluice.ner import (
    DEFAULT_LABELS,
    FALLBACK_MAX_TOKENS,
    NerSpan,
    NerTruncationError,
    ServiceInfo,
)

log = structlog.get_logger("sluice.ner.engine")

# Untere Score-Grenze des Dienstes. Der Dienst filtert bewusst nur grob vor; die
# *wirksame* Schwelle ist die profilverankerte in Sluice (§5.4). Diese Grenze muss
# deshalb immer **unter** jedem Profil-Schwellwert liegen — der Client prüft das
# fail-closed gegen `ServiceInfo.score_floor`.
DEFAULT_SCORE_FLOOR = 0.05

# Marker in den üblichen ONNX-Exportnamen → geladene Präzision. Reihenfolge zählt:
# `quint8` muss vor `int8` geprüft werden, sonst greift der kürzere Marker zuerst.
_ONNX_PRECISION_MARKERS: tuple[tuple[str, str], ...] = (
    ("quint8", "uint8"),
    ("uint8", "uint8"),
    ("int8", "int8"),
    ("fp16", "fp16"),
    ("float16", "fp16"),
    ("bf16", "bf16"),
)

# Operatoren, die nur in quantisierten Graphen vorkommen. Der Name einer Datei ist eine
# Behauptung, ihr Inhalt nicht — deshalb wird zusätzlich im Graph nachgesehen.
_QUANT_OPS: tuple[bytes, ...] = (b"QuantizeLinear", b"DynamicQuantizeLinear")


def precision_from_onnx_file(filename: str) -> str:
    """Leitet die Präzision aus dem Exportnamen ab; ohne Marker gilt `fp32` (§5.4).

    Heuristik auf den *üblichen* Exportnamen (`model.onnx`, `model_fp16.onnx`,
    `model_quint8.onnx`) — sie fängt den Normalfall, kann eine bewusst falsch benannte
    Datei aber nicht entlarven. Dafür gibt es die Inhaltsprüfung in
    `verify_onnx_precision`.
    """
    lowered = filename.lower()
    for marker, precision in _ONNX_PRECISION_MARKERS:
        if marker in lowered:
            return precision
    return "fp32"


def _graph_is_quantized(path: str) -> bool | None:
    """Sieht im ONNX-Graph nach Quantisierungs-Operatoren. `None` = nicht feststellbar.

    Stückweise gelesen: ein Export kann hunderte MB groß sein, und der Dienst soll beim
    Start nicht erst das halbe Modell in den Speicher ziehen. Der Überlappungspuffer
    verhindert, dass ein Operatorname genau auf einer Blockgrenze zerfällt.
    """
    longest = max(len(op) for op in _QUANT_OPS)
    try:
        with open(path, "rb") as handle:
            rest = b""
            while chunk := handle.read(1 << 20):
                haystack = rest + chunk
                if any(op in haystack for op in _QUANT_OPS):
                    return True
                rest = haystack[-longest:]
    except OSError as exc:  # pragma: no cover - Dateisystemfrage, nicht Logik
        log.warning("ner.onnx.unreadable", path=path, error=str(exc))
        return None
    return False


def verify_onnx_precision(
    declared: str, onnx_file: str, *, local_path: str | None = None
) -> None:
    """Prüft die deklarierte Präzision gegen die tatsächlich geladene Datei (§5.4).

    Warum das eine eigene Schranke braucht: `SLUICE_NER_PRECISION` wird frei deklariert
    und geht **unverändert in die Anonymisierungs-Identität** ein — das Profil verankert
    sie, der Client prüft sie gegen `/v1/info`, und im Audit steht am Ende, womit
    anonymisiert wurde. Welche Datei der Dienst wirklich lädt, bestimmt aber
    `SLUICE_NER_ONNX_FILE`, und zwischen beiden gab es bisher keine Verbindung.

    Dass das keine Formalie ist, zeigt die Messung vom 2026-08-11 auf der Sluice-VM:
    dasselbe Repo lieferte über `model.onnx` **89** Spans bei 4.000 Zeichen, über
    `model_quint8.onnx` **113**. Die Datei entscheidet also über das Ergebnis. Eine
    Identität, die `fp32` behauptet, während uint8 rechnet, ist damit keine ungenaue
    Angabe, sondern eine falsche.

    Zwei Prüfungen, beide fail-closed beim **Start** — nicht im Egress-Pfad:
    1. Name gegen Deklaration (fängt den Normalfall),
    2. Graph-Inhalt gegen Deklaration (fängt die falsch benannte Datei).
    """
    derived = precision_from_onnx_file(onnx_file)
    normalized = declared.strip().lower() or "fp32"
    if derived != normalized:
        raise RuntimeError(
            f"Präzision widersprüchlich: SLUICE_NER_PRECISION='{declared}', aber "
            f"SLUICE_NER_ONNX_FILE='{onnx_file}' ist '{derived}'. Die deklarierte "
            f"Präzision geht in die Anonymisierungs-Identität ein (§5.4) — sie muss der "
            f"geladenen Datei entsprechen, sonst behauptet das Audit etwas Unzutreffendes. "
            f"Entweder SLUICE_NER_PRECISION={derived} setzen oder die passende Datei wählen."
        )

    if local_path is None:
        log.info("ner.onnx.precision", declared=normalized, source="dateiname")
        return

    quantized = _graph_is_quantized(local_path)
    if quantized is None:
        log.warning("ner.onnx.graph_uncheckable", path=local_path)
        return
    declared_quantized = normalized in ("uint8", "int8")
    if quantized != declared_quantized:
        raise RuntimeError(
            f"Präzision widerspricht dem Graph: deklariert '{normalized}', der Export "
            f"'{onnx_file}' enthält {'' if quantized else 'KEINE '}Quantisierungs-"
            f"Operatoren. Der Dateiname ist eine Behauptung, der Graph nicht (§5.4)."
        )
    log.info("ner.onnx.precision", declared=normalized, source="graph")


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
        self._onnx_file = os.environ.get("SLUICE_NER_ONNX_FILE", "onnx/model.onnx")
        self._floor = _score_floor()
        self._model = self._load()
        self._max_tokens = self._detect_max_tokens()

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
            kwargs["onnx_model_file"] = self._onnx_file
            # Vor dem Laden: die geladene Datei muss zur deklarierten Präzision passen,
            # sonst behauptet die Anonymisierungs-Identität etwas Unzutreffendes (§5.4).
            verify_onnx_precision(
                self._precision,
                self._onnx_file,
                local_path=self._onnx_local_path(),
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

    def _onnx_local_path(self) -> str | None:
        """Pfad der ONNX-Datei im HF-Cache — für die Inhaltsprüfung (§5.4).

        Nur der Cache wird befragt, nie das Netz: der NER-Host soll ausgehend nichts
        brauchen (§6). Findet sich nichts, bleibt es bei der Namensprüfung.
        """
        try:
            from huggingface_hub import try_to_load_from_cache
        except ImportError:  # pragma: no cover - Umgebungsfrage
            return None
        try:
            found = try_to_load_from_cache(
                self._model_id, self._onnx_file, revision=self._revision or None
            )
        except Exception as exc:  # noqa: BLE001 - Cache-Layout ist Fremdvertrag
            log.warning("ner.onnx.cache_lookup_failed", error=str(exc))
            return None
        return found if isinstance(found, str) else None

    def _detect_max_tokens(self) -> int:
        """Das Kontextfenster des geladenen Modells (§5.3).

        GLiNER führt es als `max_len` in der Modell-Konfiguration. Findet sich dort
        nichts, wird der übliche Wert angenommen — lieber ein zu *kleines* Fenster
        annehmen als ein zu großes: eine zu vorsichtige Zerlegung kostet Latenz, eine zu
        großzügige kostet Recall, und zwar unbemerkt.
        """
        for attr in ("max_len", "max_length", "max_width"):
            value = getattr(getattr(self._model, "config", None), attr, None)
            if isinstance(value, int) and value > 0:
                return value
        log.warning("ner.max_tokens.unknown", fallback=FALLBACK_MAX_TOKENS)
        return FALLBACK_MAX_TOKENS

    def info(self) -> ServiceInfo:
        return ServiceInfo(
            model=self._model_id,
            revision=self._revision,
            precision=self._precision,
            labels=DEFAULT_LABELS,
            # Die ONNX-Datei gehört sichtbar in die Meldung: sie entscheidet über das
            # Ergebnis (2026-08-11 gemessen: 89 vs. 113 Spans je nach Export), und ohne
            # sie im Backend-String stünde im Betrieb nur „onnx" — ohne welches.
            backend=(
                f"gliner-onnx/{self._onnx_file}"
                if self._onnx
                else f"gliner-torch/{self._device}"
            ),
            score_floor=self._floor,
            max_tokens=self._max_tokens,
        )

    def detect(self, text: str, labels: tuple[str, ...]) -> list[NerSpan]:
        """Ein Text pro Aufruf — die fixierte Batchgröße ist Teil der Zusage (§5.4).

        **Kürzung ist hier ein Fehler, kein Hinweis (§5.3).** GLiNER meldet eine zu lange
        Eingabe nur als `UserWarning` und verarbeitet den Anfang — der Rest wird nie
        angesehen. Für einen Egress-Riegel ist das die gefährlichste Sorte Fehler, weil
        nichts ausfällt: Sluice bekäme Spans für den vorderen Teil und hielte den ganzen
        Text für geprüft. Deshalb wird genau diese Warnung zur Ausnahme erhoben.

        Die Prüfung sitzt bewusst hier und nicht an einer Längenschranke davor: sie greift
        am *tatsächlichen* Ereignis statt an einer Schätzung, gilt damit für jedes Modell
        und jeden Tokenizer und kann von einer falsch dimensionierten Zerlegung im Client
        nicht umgangen werden.
        """
        with warnings.catch_warnings():
            # `catch_warnings` setzt den Filterzustand zurück; sonst würde Pythons
            # "einmal pro Fundstelle"-Registry die Warnung ab dem zweiten Aufruf
            # verschlucken — und damit ausgerechnet im Dauerbetrieb.
            warnings.filterwarnings("error", message=r".*truncat.*", category=UserWarning)
            try:
                entities = self._model.predict_entities(  # type: ignore[attr-defined]
                    text, list(labels), threshold=self._floor
                )
            except UserWarning as exc:
                raise NerTruncationError(
                    f"Modell hat die Eingabe gekürzt ({len(text)} Zeichen, Fenster "
                    f"{self._max_tokens} Token): '{exc}'. Der hintere Teil wurde NICHT "
                    f"geprüft — fail-closed statt stiller Lücke (§5.3). Der Client muss "
                    f"den Text zerlegen (max_chars_per_chunk)."
                ) from exc
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
