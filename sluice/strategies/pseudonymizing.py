"""PseudonymizingStrategy — forward + reverse, explizites Opt-in (Spec §3/§8).

Herkunft: PrismClaws `prismclaw.anon` (detect.py, mapping.py, stream.py), vollständig
portiert. Verhalten erhalten; angepasst wurde nur der Storage: v1 ist `memory`
(Spec §8) — der Redis-Pfad bleibt beim heutigen Gateway und ist hier post-v1.

Pseudonyme sind stabil pro Scope (gleicher Roh-Wert → gleiches Token) und
typ-getaggt: ``⟦NAME_1⟧``, ``⟦IP_2⟧``, … Die Klammer-Glyphen überleben Tokenizer
und sind im Stream greppbar.

Mapping-Lebenszyklus (Spec §8): scope (session-gebunden), ttl_seconds, storage=memory,
deterministisches Aufräumen bei Scope-Ende (`end_scope`), keine Leaks über Scopes.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog

from sluice.strategies import EgressPayload, Sanitized, Scope

log = structlog.get_logger("sluice.strategies.pseudonymizing")

TOKEN_OPEN = "⟦"
TOKEN_CLOSE = "⟧"
TOKEN_RE = re.compile(rf"{TOKEN_OPEN}[A-Z]+_\d+{TOKEN_CLOSE}")

# Scope-Key, wenn ein Aufruf keinen Scope trägt (Mapping bleibt innerhalb des
# Prozesses stabil — Verhalten wie PrismClaws _DEFAULT_SCOPE).
_DEFAULT_SCOPE = "global"

# Längstes Token, auf das der Holdback-Puffer je wartet; darüber ist ein einzelnes ⟦ nur Text.
_MAX_TOKEN_LEN = 32


def _token(ptype: str, n: int) -> str:
    return f"{TOKEN_OPEN}{ptype}_{n}{TOKEN_CLOSE}"


# ---- Detektion (aus prismclaw/anon/detect.py) -----------------------------------------

# Reihenfolge zählt nur für die Überlappungs-Auflösung (früher = höhere Priorität).
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("MAC", re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}\b")),
    # Bewusst konservativ: internationale (+49…) oder deutsche 0-präfixierte Nummern
    # mit Trenner — nackte Ziffernfolgen bleiben unangetastet.
    ("PHONE", re.compile(r"(?<![\w.])\+\d{7,15}\b|(?<!\d)0\d{2,4}[ \-/]\d{5,9}(?!\d)")),
]


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    type: str
    value: str


class Detector:
    """Findet PII-Spans im Text. Zustandslos; über Requests teilbar."""

    def __init__(self, dictionary_terms: list[str] | tuple[str, ...] = ()) -> None:
        terms = [t.strip() for t in dictionary_terms if t.strip()]
        self._dict_re: re.Pattern[str] | None = None
        if terms:
            # Längste zuerst, damit „Musterstraße 12" über „Musterstraße" gewinnt.
            alternation = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
            self._dict_re = re.compile(rf"(?<!\w)(?:{alternation})(?!\w)", re.IGNORECASE)

    def spans(self, text: str) -> list[Span]:
        """Nicht überlappende PII-Spans, nach Position sortiert.

        Das Wörterbuch schlägt die Regex-Batterie; unter Regex-Treffern gewinnt
        der frühere/längere Match.
        """
        candidates: list[tuple[int, Span]] = []
        if self._dict_re is not None:
            for m in self._dict_re.finditer(text):
                candidates.append((0, Span(m.start(), m.end(), "NAME", m.group())))
        for prio, (ptype, pattern) in enumerate(_PATTERNS, start=1):
            for m in pattern.finditer(text):
                candidates.append((prio, Span(m.start(), m.end(), ptype, m.group())))

        candidates.sort(key=lambda c: (c[1].start, c[0], -(c[1].end - c[1].start)))
        result: list[Span] = []
        last_end = -1
        for _prio, span in candidates:
            if span.start < last_end:
                continue
            result.append(span)
            last_end = span.end
        return result


# ---- Streaming-Holdback (aus prismclaw/anon/stream.py) --------------------------------


class StreamReverser:
    """Chunks rein, de-pseudonymisierter Text raus — mit Holdback-Puffer.

    Ein Pseudonym wie ``⟦PERSON_1⟧`` kann über zwei Stream-Chunks gesplittet ankommen
    (``…⟦PER`` + ``SON_1⟧…``). `feed` hält deshalb einen angebrochenen Token-Kandidaten
    zurück und gibt ihn frei, sobald er entweder komplett ist (→ zurückgemappt) oder
    sich als Nicht-Token entpuppt. Kosten: wenige Zeichen Latenz, nur solange die
    öffnende Glyphe offen ist. (Kritischer Failure-Mode Streaming-Passthrough, §7.2.)
    """

    def __init__(self, lookup: Callable[[re.Match[str]], str] | None) -> None:
        self._lookup = lookup
        self._buf = ""

    def feed(self, chunk: str) -> str:
        """Verarbeitet einen Chunk; gibt zurück, was jetzt sicher emittierbar ist (ggf. '')."""
        self._buf += chunk
        if self._lookup is None:
            out, self._buf = self._buf, ""
            return out
        text = TOKEN_RE.sub(self._lookup, self._buf)
        i = text.rfind(TOKEN_OPEN)
        holding = i != -1 and TOKEN_CLOSE not in text[i:] and len(text) - i < _MAX_TOKEN_LEN
        if holding:
            self._buf = text[i:]
            return text[:i]
        self._buf = ""
        return text

    def flush(self) -> str:
        """Gibt frei, was am Stream-Ende noch zurückgehalten wurde."""
        out, self._buf = self._buf, ""
        if self._lookup is not None and out:
            out = TOKEN_RE.sub(self._lookup, out)
        return out


# ---- Scope-Mapping (aus prismclaw/anon/mapping.py, storage=memory) --------------------


class ScopeMap:
    """Pseudonym-Map für einen Scope (Session). Wird nie über Scopes geteilt (§4.4/§8)."""

    def __init__(self, *, scope: str, detector: Detector) -> None:
        self._scope = scope
        self._detector = detector
        self._rev: dict[str, str] = {}  # token -> real
        self._fwd: dict[str, str] = {}  # real -> token
        self._counters: dict[str, int] = {}

    def _register(self, token: str, real: str) -> None:
        self._rev[token] = real
        self._fwd[real] = token
        m = re.match(rf"{TOKEN_OPEN}([A-Z]+)_(\d+){TOKEN_CLOSE}", token)
        if m:
            ptype, n = m.group(1), int(m.group(2))
            self._counters[ptype] = max(self._counters.get(ptype, 0), n)

    # ---- forward (roh -> Pseudonym) ----------------------------------------------------

    def forward_text(self, text: str | None) -> str | None:
        """Pseudonymisiert alle erkannten PII in ``text``."""
        if not text:
            return text
        spans = self._detector.spans(text)
        if not spans:
            return text
        out: list[str] = []
        pos = 0
        for span in spans:
            token = self._fwd.get(span.value)
            if token is None:
                self._counters[span.type] = self._counters.get(span.type, 0) + 1
                token = _token(span.type, self._counters[span.type])
                self._register(token, span.value)
            out.append(text[pos : span.start])
            out.append(token)
            pos = span.end
        out.append(text[pos:])
        return "".join(out)

    def forward_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Pseudonymisiert die ``content``-Strings von role/content-Messages."""
        out = []
        for m in messages:
            content = m.get("content")
            if isinstance(content, str):
                m = {**m, "content": self.forward_text(content)}
            out.append(m)
        return out

    # ---- reverse (Pseudonym -> roh) ----------------------------------------------------

    def _lookup(self, m: re.Match[str]) -> str:
        return self._rev.get(m.group(), m.group())

    def reverse_text(self, text: str | None) -> str | None:
        if not text or TOKEN_OPEN not in text:
            return text
        return TOKEN_RE.sub(self._lookup, text)

    def reverse_obj(self, obj: Any) -> Any:
        """Rekursiv alle Strings eines JSON-artigen Werts zurückmappen.

        Für modellproduzierte Tool-Call-Argumente, damit Tools immer auf echten
        Werten laufen (kritischer Failure-Mode Tool-Call-Passthrough, §7.2).
        """
        if isinstance(obj, str):
            return self.reverse_text(obj)
        if isinstance(obj, dict):
            return {k: self.reverse_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.reverse_obj(v) for v in obj]
        return obj

    def stream_reverser(self) -> StreamReverser:
        """Ein Holdback-Puffer, gebunden an die Reverse-Map dieses Scopes."""
        return StreamReverser(self._lookup)


# ---- Die Strategie (Spec §3, reversible=True) ------------------------------------------


@dataclass
class _ScopeEntry:
    map: ScopeMap
    created: float


class PseudonymizingStrategy:
    """reversible=True — explizites Opt-in (Spec §2): schwächere DSGVO-Zusage
    (die Mapping-Tabelle bleibt personenbezogen) und session-scoped Zustand (§8).
    """

    reversible = True
    name = "pseudonymizing"

    def __init__(
        self,
        *,
        dictionary_terms: tuple[str, ...] | list[str] = (),
        ttl_seconds: int = 3600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._detector = Detector(dictionary_terms)
        self._ttl = ttl_seconds
        self._clock = clock
        self._scopes: dict[str, _ScopeEntry] = {}

    # ---- Mapping-Lebenszyklus (§8) -----------------------------------------------------

    def _scope_map(self, scope: Scope | None) -> ScopeMap:
        """Map für einen Scope; legt an, erneuert nach TTL-Ablauf, teilt nie über Scopes."""
        key = scope.key if scope is not None else _DEFAULT_SCOPE
        entry = self._scopes.get(key)
        now = self._clock()
        if entry is not None and now - entry.created >= self._ttl:
            # TTL abgelaufen → Mapping deterministisch verwerfen (§8).
            del self._scopes[key]
            entry = None
            log.info("pseudonymizing.scope_expired", scope=key)
        if entry is None:
            entry = _ScopeEntry(map=ScopeMap(scope=key, detector=self._detector), created=now)
            self._scopes[key] = entry
        return entry.map

    def end_scope(self, scope: Scope) -> None:
        """Deterministisches Aufräumen bei Scope-Ende (§8) — kein Leak über Sessions."""
        if self._scopes.pop(scope.key, None) is not None:
            log.info("pseudonymizing.scope_ended", scope=scope.key)

    # ---- SanitizationStrategy (§3) -----------------------------------------------------

    async def forward(self, payload: EgressPayload, scope: Scope | None) -> Sanitized:
        """Roh → pseudonymisiert; konsistentes Pseudonym pro Roh-Wert innerhalb des Scope."""
        scope_map = self._scope_map(scope)
        if payload.messages is not None:
            return Sanitized(messages=scope_map.forward_messages(payload.messages))
        return Sanitized(text=scope_map.forward_text(payload.raw_text))

    async def reverse_text(self, text: str, scope: Scope) -> str:
        result = self._scope_map(scope).reverse_text(text)
        return result if result is not None else text

    def stream_reverser(self, scope: Scope) -> StreamReverser:
        return self._scope_map(scope).stream_reverser()

    async def reverse_obj(self, obj: dict[str, Any], scope: Scope) -> dict[str, Any]:
        return self._scope_map(scope).reverse_obj(obj)
