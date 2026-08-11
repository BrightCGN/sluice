"""Span-Mechanik — Vereinigung, Vorrang, Redaktion (Spec §5.3, Rev. 12).

Die Regeln hier entscheiden, was am Ende maskiert wird. Sie sind bewusst ohne Modell und
ohne Dienst testbar: die Vereinigungslogik ist reiner Mechanismus (§1.1).
"""

from __future__ import annotations

from sluice.spans import SOURCE_NER, SOURCE_REGEX, Span, apply_spans, merge_spans


def regex_span(start: int, end: int, label: str = "[IBAN]") -> Span:
    return Span(start, end, label, SOURCE_REGEX, "regex-Treffer")


def ner_span(start: int, end: int, label: str = "[PERSON]", score: float = 0.9) -> Span:
    return Span(start, end, label, SOURCE_NER, "ner-Treffer", score)


def test_disjunkte_spans_werden_vereinigt() -> None:
    merged = merge_spans([regex_span(0, 5)], [ner_span(10, 20)])
    assert [(s.start, s.end) for s in merged] == [(0, 5), (10, 20)]


def test_regex_gewinnt_gegen_ueberlappenden_ner_span() -> None:
    """Nur die Regex-Stufe kennt den validierten Typ (§5.3)."""
    merged = merge_spans([regex_span(10, 30)], [ner_span(15, 22)])
    assert len(merged) == 1
    assert merged[0].source == SOURCE_REGEX


def test_regex_gewinnt_auch_wenn_der_ner_span_laenger_ist() -> None:
    """Der Vorrang ist kategorisch, nicht längenabhängig — sonst könnte ein breiter
    Modell-Span den typisierten Platzhalter überschreiben."""
    merged = merge_spans([regex_span(10, 15)], [ner_span(5, 40)])
    assert len(merged) == 1
    assert merged[0].label == "[IBAN]"


def test_ner_span_der_nur_angrenzt_bleibt_erhalten() -> None:
    """Halboffene Intervalle: [0,5) und [5,9) überlappen sich nicht."""
    merged = merge_spans([regex_span(0, 5)], [ner_span(5, 9)])
    assert len(merged) == 2


def test_bei_ner_ueberlappung_gewinnt_der_laengere_span() -> None:
    """Mehr maskiert = mehr Recall (§5.4)."""
    merged = merge_spans([], [ner_span(10, 15), ner_span(10, 25)])
    assert len(merged) == 1
    assert (merged[0].start, merged[0].end) == (10, 25)


def test_reihenfolge_der_eingabe_aendert_das_ergebnis_nicht() -> None:
    """Determinismus (§5.4): das Ergebnis darf nicht an der Eingabereihenfolge hängen."""
    a = [ner_span(30, 40), ner_span(10, 20), ner_span(0, 5)]
    b = list(reversed(a))
    assert merge_spans([], a) == merge_spans([], b)


def test_apply_spans_ersetzt_von_rechts_nach_links() -> None:
    text = "Konto DE89370400440532013000 von Max Mustermann"
    spans = [
        regex_span(6, 28),
        ner_span(33, 47),
    ]
    assert apply_spans(text, merge_spans([spans[0]], [spans[1]])) == "Konto [IBAN] von [PERSON]"


def test_apply_spans_ist_stabil_bei_unterschiedlichen_laengen() -> None:
    """Ein längerer Platzhalter als Original darf spätere Offsets nicht verschieben."""
    text = "ab XY cd"
    result = apply_spans(text, [Span(3, 5, "[SEHR_LANGER_PLATZHALTER]", SOURCE_NER, "x")])
    assert result == "ab [SEHR_LANGER_PLATZHALTER] cd"


def test_leere_span_menge_laesst_den_text_unveraendert() -> None:
    assert apply_spans("unverändert", []) == "unverändert"
