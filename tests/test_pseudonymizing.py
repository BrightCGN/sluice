"""PseudonymizingMode — portierte PrismClaw-`anon`-Suite + Mapping-Lebenszyklus (§8).

Herkunft: prismclaw backend/claude/tests/test_anon.py. Pure In-Memory-Tests
(storage="memory" ist v1, Spec §8). Dazu die Pflicht-Fälle Scope-Isolation und
TTL-Ablauf aus der Definition of Done.
"""

from __future__ import annotations

from sluice.modes import EgressPayload, Scope
from sluice.modes.pseudonymizing import Detector, PseudonymizingMode


def _mode(terms: list[str] | None = None, **kwargs) -> PseudonymizingMode:
    return PseudonymizingMode(dictionary_terms=terms or [], **kwargs)


# ---------- Detektion (portiert) ----------


def test_detects_email_ip_mac() -> None:
    d = Detector([])
    text = "Mail an r.cochius@gmail.com, Gerät 192.168.50.21 (AA:BB:CC:DD:EE:FF)"
    types = {s.type for s in d.spans(text)}
    assert types == {"EMAIL", "IP", "MAC"}

def test_dictionary_case_insensitive_and_longest_first() -> None:
    d = Detector(["Richard", "Musterstraße 12", "Musterstraße"])
    spans = d.spans("richard wohnt in der Musterstraße 12.")
    assert [(s.type, s.value) for s in spans] == [
        ("NAME", "richard"),
        ("NAME", "Musterstraße 12"),
    ]


def test_plain_numbers_not_matched_as_phone() -> None:
    d = Detector([])
    assert d.spans("Die Rechnung über 12345678 Euro von 2026") == []


# ---------- forward / reverse (portiert) ----------


async def test_forward_reverse_roundtrip() -> None:
    s = _mode(["Richard"])
    scope = Scope("test")
    text = "Richard (r.cochius@gmail.com) meldet 192.168.50.21 offline."
    fwd = (await s.forward(EgressPayload(raw_text=text), scope)).text
    assert "Richard" not in fwd
    assert "gmail" not in fwd
    assert "192.168" not in fwd
    assert "⟦NAME_1⟧" in fwd and "⟦EMAIL_1⟧" in fwd and "⟦IP_1⟧" in fwd
    assert await s.reverse_text(fwd, scope) == text


async def test_pseudonyms_stable_within_scope() -> None:
    s = _mode(["Richard"])
    scope = Scope("test")
    a = (await s.forward(EgressPayload(raw_text="Richard hier."), scope)).text
    b = (await s.forward(EgressPayload(raw_text="Nochmal Richard."), scope)).text
    assert "⟦NAME_1⟧" in a and "⟦NAME_1⟧" in b


async def test_forward_messages_only_touches_content() -> None:
    s = _mode(["Richard"])
    sanitized = await s.forward(
        EgressPayload(raw_text="", messages=[{"role": "user", "content": "Ich bin Richard"}]),
        Scope("test"),
    )
    assert sanitized.messages[0]["role"] == "user"
    assert "Richard" not in sanitized.messages[0]["content"]


# ---------- Pflicht-Fall: Tool-Arg-Reversal (§7.2) ----------


async def test_reverse_obj_recurses_tool_args() -> None:
    s = _mode([])
    scope = Scope("test")
    fwd = (await s.forward(EgressPayload(raw_text="mail an r.cochius@gmail.com"), scope)).text
    token = [w for w in fwd.split() if w.startswith("⟦")][0]
    args = {"to": token, "nested": {"cc": [token]}, "n": 3}
    real = await s.reverse_obj(args, scope)
    assert real["to"] == "r.cochius@gmail.com"  # echte Werte bei der (gemockten) Ausführung
    assert real["nested"]["cc"] == ["r.cochius@gmail.com"]
    assert real["n"] == 3


# ---------- Pflicht-Fall: Streaming-Holdback (§7.2, portiert) ----------


async def test_stream_reverser_token_split_across_chunks() -> None:
    s = _mode(["Richard"])
    scope = Scope("test")
    await s.forward(EgressPayload(raw_text="Richard"), scope)  # erzeugt ⟦NAME_1⟧
    r = s.stream_reverser(scope)
    out = r.feed("Hallo ⟦NA") + r.feed("ME_1⟧, wie geht's?") + r.flush()
    assert out == "Hallo Richard, wie geht's?"


async def test_stream_reverser_flush_trailing_token() -> None:
    s = _mode(["Richard"])
    scope = Scope("test")
    await s.forward(EgressPayload(raw_text="Richard"), scope)
    r = s.stream_reverser(scope)
    out = r.feed("Gruß an ⟦NAME_1⟧") + r.flush()
    assert out == "Gruß an Richard"


def test_stream_reverser_lone_bracket_is_plain_text() -> None:
    s = _mode([])
    r = s.stream_reverser(Scope("test"))
    out = r.feed("Mathe: ⟦ ist nur ein Zeichen, " + "x" * 40) + r.flush()
    assert out.startswith("Mathe: ⟦ ist nur ein Zeichen")


# ---------- Pflicht-Fall: Scope-Isolation (§8/§4.4) ----------


async def test_scopes_share_no_mapping() -> None:
    """Zwei Scopes teilen kein Mapping: das Token aus Scope A ist in Scope B wertlos."""
    s = _mode(["Richard"])
    a, b = Scope("session-a"), Scope("session-b")
    fwd_a = (await s.forward(EgressPayload(raw_text="Richard meldet sich"), a)).text
    token = "⟦NAME_1⟧"
    assert token in fwd_a
    # Scope B kennt das Mapping nicht → Token bleibt unaufgelöst, kein Leak.
    assert await s.reverse_text(f"Hallo {token}", b) == f"Hallo {token}"
    # Gleicher Roh-Wert in B erzeugt einen eigenen, unabhängigen Mapping-Eintrag.
    fwd_b = (await s.forward(EgressPayload(raw_text="Anruf von Richard"), b)).text
    assert "Richard" not in fwd_b
    assert await s.reverse_text(fwd_b, b) == "Anruf von Richard"
    assert await s.reverse_text(fwd_a, a) == "Richard meldet sich"


# ---------- Pflicht-Fall: TTL-Ablauf + Scope-Ende (§8) ----------


async def test_mapping_discarded_after_ttl() -> None:
    now = [0.0]
    s = _mode(["Richard"], ttl_seconds=3600, clock=lambda: now[0])
    scope = Scope("session")
    fwd = (await s.forward(EgressPayload(raw_text="Richard"), scope)).text
    assert "⟦NAME_1⟧" in fwd
    now[0] = 3601.0  # TTL überschritten → Mapping deterministisch verworfen
    assert await s.reverse_text(fwd, scope) == fwd


async def test_end_scope_cleans_up_deterministically() -> None:
    s = _mode(["Richard"])
    scope = Scope("session")
    fwd = (await s.forward(EgressPayload(raw_text="Richard"), scope)).text
    s.end_scope(scope)
    assert await s.reverse_text(fwd, scope) == fwd  # kein Leak über Session-Ende hinaus
