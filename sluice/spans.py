"""Span-Mechanik — Offset-basierte Erkennung und Redaktion (Spec §5.3, Rev. 12).

Warum Spans statt sequentieller `re.sub`-Ketten (`verifier.redact_identifiers`): sobald
**zwei Erkennungsstufen** (Regex + NER, §5.3) dasselbe Textfeld beanspruchen, braucht es
eine gemeinsame Koordinate. Die ist der **Zeichen-Offset** — nicht der Token-Offset, denn
Sluice redigiert Zeichen, und die Token-Grenzen des Modells sind Engine-Detail (§5.1).

Drei Schritte, alle deterministisch (§5.4):
1. `detect_regex_spans` — Muster des Detektor-Profils + Wörterbuch als Spans.
2. `merge_spans`        — Vereinigungsmenge; **Regex hat Vorrang** bei Überlappung,
                          weil nur die Regex-Stufe den *validierten Typ* kennt (§5.3).
3. `apply_spans`        — Ersetzung von rechts nach links, damit frühere Offsets gültig
                          bleiben.

Die Sortierung ist total und stabil (`_order`), damit gleiche Eingabe über Prozess-
neustarts hinweg dieselbe Ausgabe liefert — die Determinismus-Zusage aus §5.4 hängt
daran genauso wie an der fixierten Batchgröße im NER-Dienst.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sluice.detectors import (
    DetectorProfile,
    build_dictionary_patterns,
    get_detector_profile,
)

# Herkunft eines Spans. Die Regex-Stufe ist die *validierende* (Prüfsummen, §5.3) und
# gewinnt deshalb jede Überlappung gegen das Modell.
SOURCE_REGEX = "regex"
SOURCE_NER = "ner"


@dataclass(frozen=True)
class Span:
    """Ein erkannter Bereich im Text — Zeichen-Offsets, halboffen `[start, end)`.

    label:   typisierter Platzhalter, der beim Redigieren eingesetzt wird (`[IBAN]`).
    source:  `regex` (validierter Typ, Vorrang) oder `ner` (Modell, §5.3).
    finding: Klartext-Befund für Blockier-Grund und Audit (§6).
    score:   Modell-Konfidenz; die Regex-Stufe setzt 1.0 (deterministisch validiert).
    """

    start: int
    end: int
    label: str
    source: str
    finding: str
    score: float = 1.0

    def overlaps(self, other: Span) -> bool:
        return self.start < other.end and other.start < self.end


def _order(span: Span) -> tuple[int, int, str, str]:
    """Totale Ordnung: Position, dann *längerer* Span zuerst, dann Label/Quelle.

    Ohne den Label-/Quellen-Tiebreak wären zwei gleich lange Spans an derselben Stelle
    nur zufällig geordnet — und die Ausgabe damit nicht reproduzierbar (§5.4).
    """
    return (span.start, -span.end, span.label, span.source)


# ---- 1. Regex-Stufe -----------------------------------------------------------------


def detect_regex_spans(
    text: str,
    detector_profile: str | DetectorProfile,
    *,
    dictionary_terms: Sequence[str] = (),
) -> tuple[Span, ...]:
    """Fährt die Muster des Detektor-Profils über den Text und liefert Spans (§5.1).

    Unbekanntes Profil → **leere** Span-Menge. Das ist kein stiller Durchlass: der
    Verifier im Guard blockt danach fail-closed über dasselbe unbekannte Profil (§5),
    genau wie `redact_identifiers` es hält.

    `validate` (Prüfsummen wie Mod-97/Luhn, §5.3) wird hier **immer** ausgewertet — anders
    als in `verify_no_identifiers`, wo ein Validator ohne `per_match=True` wirkungslos
    bliebe. Muster mit Validator müssen deshalb `per_match=True` setzen (siehe pii_de).
    """
    if isinstance(detector_profile, str):
        profile = get_detector_profile(detector_profile)
        if profile is None:
            return ()
    else:
        profile = detector_profile

    spans: list[Span] = []
    for deny in (*profile.deny, *build_dictionary_patterns(dictionary_terms)):
        for match in deny.pattern.finditer(text):
            value = match.group(0)
            if deny.validate is not None and not deny.validate(value):
                continue
            spans.append(
                Span(
                    start=match.start(),
                    end=match.end(),
                    label=deny.placeholder,
                    source=SOURCE_REGEX,
                    finding=deny.finding,
                )
            )
    return tuple(sorted(spans, key=_order))


# ---- 2. Vereinigung -----------------------------------------------------------------


def merge_spans(
    regex_spans: Iterable[Span], ner_spans: Iterable[Span] = ()
) -> tuple[Span, ...]:
    """Vereinigungsmenge beider Stufen — **Regex vor NER** bei Überlappung (§5.3).

    Der Vorrang ist inhaltlich begründet, nicht willkürlich: die Regex-Stufe kennt den
    per Prüfsumme *validierten* Typ (`[IBAN]`), das Modell nur eine Label-Vermutung.
    Ein NER-Span, der einen Regex-Span auch nur berührt, fällt deshalb ganz weg — er
    würde sonst den typisierten Platzhalter überschreiben oder zerschneiden.

    Innerhalb der NER-Stufe gewinnt der **längere** Span (mehr maskiert = mehr Recall,
    §5.4); bei gleicher Länge entscheidet die totale Ordnung `_order`.
    """
    kept: list[Span] = sorted(regex_spans, key=_order)

    # Regex-Spans können untereinander überlappen (z. B. Telefon in Freitext). Auch hier
    # gewinnt der längere — sonst hinge das Ergebnis an der Muster-Reihenfolge im Profil.
    kept = _resolve_overlaps(kept)

    for candidate in sorted(ner_spans, key=_order):
        if any(candidate.overlaps(existing) for existing in kept):
            continue
        kept.append(candidate)
        kept = _resolve_overlaps(sorted(kept, key=_order))

    return tuple(sorted(kept, key=_order))


def _resolve_overlaps(spans: list[Span]) -> list[Span]:
    """Behält aus jeder Überlappungsgruppe den ersten nach `_order` (= längsten)."""
    resolved: list[Span] = []
    for span in spans:
        if any(span.overlaps(existing) for existing in resolved):
            continue
        resolved.append(span)
    return resolved


# ---- 3. Redaktion -------------------------------------------------------------------


def apply_spans(text: str, spans: Sequence[Span]) -> str:
    """Ersetzt jeden Span durch sein Label — von rechts nach links (Offsets bleiben gültig)."""
    result = text
    for span in sorted(spans, key=_order, reverse=True):
        result = result[: span.start] + span.label + result[span.end :]
    return result


def span_findings(spans: Sequence[Span]) -> tuple[str, ...]:
    """Befunde für das Audit (§6) — ohne die Rohwerte selbst zu transportieren."""
    return tuple(
        f"{span.finding} [{span.source}@{span.start}:{span.end}]" for span in spans
    )


__all__ = [
    "SOURCE_NER",
    "SOURCE_REGEX",
    "Span",
    "apply_spans",
    "detect_regex_spans",
    "merge_spans",
    "span_findings",
]
