"""Anonymisierungs-Identität — was eine Sanitisierung reproduzierbar macht (Spec §5.4, Rev. 12).

Die Frage, die diese Datei beantwortet: *„Womit genau wurde dieser Text anonymisiert?"*
Sie ist die Voraussetzung für **verifizierbare** Anonymisierung — ohne sie ist ein
Audit-Eintrag nur die Behauptung, dass sanitisiert wurde, ohne prüfbaren Bezug darauf,
**womit**.

Die Identität bündelt alles, was das Ergebnis verändern kann, an *einer* Stelle:
- der Modus (§3) und sein Detektor-Profil (§5.1),
- das Profil-Wörterbuch, als Digest — die Terme selbst sind personenbezogen (Rev. 10)
  und dürfen deshalb nie ins Audit; ihre *Änderung* muss aber sichtbar sein,
- bei NER-Modi: Modell-Repo **und Revision-Hash**, geladene Präzision, Schwellwert,
  Label-Liste und die fixierte Batchgröße (§5.4).

Verankert ist sie im Profil, analog zum Modus-Schalter selbst (§4): dieselbe Stelle, die
entscheidet *ob* sanitisiert wird, legt auch fest *wie genau*.

`digest()` ist der kurze, vergleichbare Fingerabdruck: gleiche Identität ⇒ gleicher
Digest ⇒ bei gleicher Eingabe dieselben Spans. Ändert sich der Schwellwert oder die
Modell-Revision, ändert sich der Digest — ein stiller Modellwechsel wird damit im Audit
sichtbar, statt unbemerkt die Zusage zu verschieben.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sluice.ner import FIXED_BATCH_SIZE, NerConfig


@dataclass(frozen=True)
class NerIdentity:
    """Der NER-Anteil der Identität (§5.4)."""

    model_repo: str
    model_revision: str
    model_precision: str
    threshold: float
    labels: tuple[str, ...]
    batch_size: int = FIXED_BATCH_SIZE
    threshold_calibrated: bool = False
    # Zerlegung langer Texte (§5.3). Gehört in die Identität, weil andere Schnitte zu
    # anderen Spans führen: das Modell sieht je Stück einen anderen Kontext, und eine
    # Entität an der Schnittstelle hängt an der Überlappung. Ohne diese beiden Werte
    # wäre „gleicher Digest ⇒ gleiche Spans" für lange Texte schlicht unwahr.
    max_chars_per_chunk: int = 0
    chunk_overlap_chars: int = 0

    @classmethod
    def from_config(cls, config: NerConfig) -> NerIdentity:
        return cls(
            model_repo=config.model_repo,
            model_revision=config.model_revision,
            model_precision=config.model_precision,
            threshold=config.threshold,
            labels=tuple(config.labels),
            threshold_calibrated=config.threshold_declared,
            max_chars_per_chunk=config.max_chars_per_chunk,
            chunk_overlap_chars=config.chunk_overlap_chars,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_repo": self.model_repo,
            "model_revision": self.model_revision,
            "model_precision": self.model_precision,
            "threshold": self.threshold,
            "labels": list(self.labels),
            "batch_size": self.batch_size,
            "threshold_calibrated": self.threshold_calibrated,
            "max_chars_per_chunk": self.max_chars_per_chunk,
            "chunk_overlap_chars": self.chunk_overlap_chars,
        }


@dataclass(frozen=True)
class AnonymizationIdentity:
    """Die vollständige, profilverankerte Anonymisierungs-Identität (§5.4)."""

    mode: str
    detector_profile: str
    dictionary_digest: str
    ner: NerIdentity | None = None

    def as_dict(self) -> dict[str, Any]:
        """Klartext-Form fürs Audit und `GET /v1/anonymization-identity`.

        Modellversion und Schwellwert stehen hier ausdrücklich *ableitbar* drin — das ist
        das Akzeptanzkriterium, nicht nur der Digest.
        """
        payload: dict[str, Any] = {
            "mode": self.mode,
            "detector_profile": self.detector_profile,
            "dictionary_digest": self.dictionary_digest,
        }
        if self.ner is not None:
            payload["ner"] = self.ner.as_dict()
        return payload

    def digest(self) -> str:
        """Stabiler Fingerabdruck (sha256, kanonisches JSON) — vergleichbar über Läufe."""
        material = json.dumps(self.as_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def short(self) -> str:
        """Kurzform fürs Log — reicht, um einen Wechsel zu erkennen."""
        return self.digest()[:16]


def digest_terms(terms: tuple[str, ...]) -> str:
    """Digest der Wörterbuch-Terme (Rev. 10) — die Terme selbst bleiben draußen.

    Sortiert, damit eine bloße Umordnung im Profil die Identität nicht ändert: die
    Reihenfolge hat für das literale Matching keine Wirkung (§5.1.1), also darf sie auch
    den Fingerabdruck nicht bewegen.
    """
    if not terms:
        return "none"
    # Kanonisches JSON statt Trennzeichen-Join: ein Term darf jedes Zeichen
    # enthalten, und zwei verschiedene Term-Listen dürfen nie denselben
    # Digest ergeben.
    material = json.dumps(sorted(terms), ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def build_identity(
    *,
    mode: str,
    detector_profile: str,
    dictionary_terms: tuple[str, ...] = (),
    ner_config: NerConfig | None = None,
) -> AnonymizationIdentity:
    """Baut die Identität aus den profilverankerten Werten (§4/§5.4)."""
    return AnonymizationIdentity(
        mode=mode,
        detector_profile=detector_profile,
        dictionary_digest=digest_terms(dictionary_terms),
        ner=NerIdentity.from_config(ner_config) if ner_config is not None else None,
    )


__all__ = [
    "AnonymizationIdentity",
    "NerIdentity",
    "build_identity",
    "digest_terms",
]
