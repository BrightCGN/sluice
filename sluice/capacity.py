"""Kapazitäts-Telemetrie — was ein Provider über seinen eigenen Kopfstand meldet
(Spec §7.6, Revision 16).

Bis Rev. 15 warf Sluice diese Auskunft weg: `ProviderResponse` trug nur
text/model/provider, der Gateway-Vertrag `/v1/complete` (§7.4) gab nichts darüber
zurück. Damit war die einzige Stelle, an der *alle* Egress-Aufrufe vorbeikommen,
zugleich die einzige, die nicht sagen konnte, wie voll die Provider-Konten sind.

Drei Regeln, die dieses Modul trägt:

1. **Unbekannt ist `None`, nie `0`.** Ein Provider ohne Rate-Limit-Header (Gemini)
   meldet keinen Kopfstand — das ist etwas anderes als „Kontingent aufgebraucht".
   Ein Wert, den niemand gemessen hat, darf hier nicht wie ein gemessener aussehen.
2. **Best-effort und pro Instanz.** Der Stand lebt in-memory im Prozess und ist
   ausdrücklich *keine* Zusage: er ist so alt wie der letzte Aufruf (`observed_at`),
   und mehrere Kern-Instanzen sehen jeweils nur ihre eigenen. Wer daraus eine
   Abrechnung bauen will, ist hier falsch.
3. **Telemetrie ändert nie eine Egress-Entscheidung im Sinne von §2.** Sie steuert
   höchstens die *Auswahl innerhalb* der Profil-Allowlist (`sluice/rotation.py`,
   §4.5) — Gate, Modus, Verifier und Audit laufen davon unberührt. Fällt die
   Telemetrie aus, wird nichts durchlässiger, nur die Verteilung gleichförmiger.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

log = structlog.get_logger("sluice.capacity")


@dataclass(frozen=True)
class Usage:
    """Token-Verbrauch eines einzelnen Aufrufs (§7.6).

    Die Namen sind die neutrale Form; die Adapter übersetzen aus dem jeweiligen
    Provider-Dialekt (`input_tokens`/`prompt_tokens`/`promptTokenCount`).
    """

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def merged_with(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class RateLimit:
    """Der vom Provider gemeldete Kopfstand (§7.6).

    Jedes Feld ist optional: kein Provider meldet alle, und ein fehlendes Feld ist
    **unbekannt**, nicht null. `headroom()` liefert deshalb `None`, solange nichts
    Belastbares vorliegt — ein geratener Headroom wäre schlimmer als keiner, weil
    die Auswahl (§4.5) ihn nicht von einem gemessenen unterscheiden könnte.
    """

    requests_limit: int | None = None
    requests_remaining: int | None = None
    tokens_limit: int | None = None
    tokens_remaining: int | None = None
    reset_seconds: float | None = None

    def headroom(self) -> float | None:
        """Relativer Restanteil in `[0,1]` — das **Minimum** über alle bekannten Achsen.

        Minimum, weil das knappste Kontingent bindet: wer noch 90 % seiner Tokens,
        aber nur 2 % seiner Requests hat, ist bei 2 %. Sind beide Achsen unbekannt,
        ist der Headroom unbekannt (`None`).
        """
        ratios: list[float] = []
        for limit, remaining in (
            (self.requests_limit, self.requests_remaining),
            (self.tokens_limit, self.tokens_remaining),
        ):
            if limit is not None and remaining is not None and limit > 0:
                ratios.append(max(0.0, min(1.0, remaining / limit)))
        return min(ratios) if ratios else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests_limit": self.requests_limit,
            "requests_remaining": self.requests_remaining,
            "tokens_limit": self.tokens_limit,
            "tokens_remaining": self.tokens_remaining,
            "reset_seconds": self.reset_seconds,
            "headroom": self.headroom(),
        }


@dataclass
class StreamTelemetry:
    """Senke für die Kapazitätsdaten eines **streamenden** Aufrufs (§7.6).

    Ein Stream liefert Text-Deltas; Verbrauch und Kopfstand fallen erst am Ende an
    (bzw. stehen in den Antwort-Headern). Ohne diese Senke müsste `stream()` zwei
    verschiedene Dinge yielden — dann trüge jeder Konsument des Iterators die
    Unterscheidung, und ein vergessener Zweig wäre ein stiller Datenverlust.

    Sie ist bewusst **mutierbar und optional**: ein Adapter, der nichts messen kann,
    lässt sie leer, und der Aufrufer sieht `None` statt einer erfundenen Null.
    """

    usage: Usage | None = None
    rate_limit: RateLimit | None = None


@dataclass(frozen=True)
class CapacityRecord:
    """Der Stand für ein (Provider, Modell)-Paar in dieser Instanz."""

    provider: str
    model: str
    calls: int
    usage: Usage
    rate_limit: RateLimit | None
    observed_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "calls": self.calls,
            "usage": self.usage.as_dict(),
            "rate_limit": self.rate_limit.as_dict() if self.rate_limit else None,
            "observed_at": self.observed_at.isoformat(),
        }


class CapacityStore:
    """In-memory Kapazitätsstand, pro Instanz, best-effort (§7.6).

    Kein Persistenz-Versprechen und keine Instanz-übergreifende Sicht: ein Neustart
    setzt den Stand zurück, zwei Kern-Instanzen sehen verschiedene Wirklichkeiten.
    Das ist Absicht — der Alternative (geteilter Zustand mit eigener Konsistenz-
    Maschinerie) stünde kein Gewinn gegenüber, der den Riegel besser macht.
    """

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], CapacityRecord] = {}

    def record(
        self,
        *,
        provider: str,
        model: str,
        usage: Usage | None,
        rate_limit: RateLimit | None,
    ) -> None:
        """Verbucht einen Aufruf. Verbrauch summiert sich, der Kopfstand ist der jüngste.

        Ein Aufruf ganz ohne Telemetrie zählt trotzdem als Aufruf (`calls`) — dass
        gerufen wurde, ist selbst eine Auskunft, und ein Provider, der nichts meldet,
        soll nicht so aussehen, als wäre er nie benutzt worden.
        """
        key = (provider, model)
        previous = self._records.get(key)
        self._records[key] = CapacityRecord(
            provider=provider,
            model=model,
            calls=(previous.calls if previous else 0) + 1,
            usage=(previous.usage if previous else Usage()).merged_with(usage or Usage()),
            # Ein neuer Kopfstand ersetzt den alten; **kein** Kopfstand lässt den
            # alten stehen (er ist die letzte belastbare Messung, `observed_at` sagt
            # wie alt) statt ihn auf None zurückzusetzen.
            rate_limit=rate_limit if rate_limit is not None else (previous.rate_limit if previous else None),
            observed_at=datetime.now(UTC),
        )

    def headroom(self, provider: str) -> float | None:
        """Relativer Headroom eines Providers — die **jüngste** Messung über seine Modelle.

        Rate-Limits gelten in aller Regel je Konto/Key, nicht je Modell; über mehrere
        Modelle desselben Providers ist die jüngste Beobachtung deshalb die beste
        verfügbare Näherung. `None` heißt: für diesen Provider liegt nichts vor.
        """
        candidates = [
            r for r in self._records.values() if r.provider == provider and r.rate_limit is not None
        ]
        if not candidates:
            return None
        newest = max(candidates, key=lambda r: r.observed_at)
        assert newest.rate_limit is not None
        return newest.rate_limit.headroom()

    def snapshot(self) -> tuple[CapacityRecord, ...]:
        """Read-only Sicht, jüngste Beobachtung zuerst."""
        return tuple(sorted(self._records.values(), key=lambda r: r.observed_at, reverse=True))

    def reset(self) -> None:
        """Nur für Tests — im Betrieb ist der Stand rein additiv."""
        self._records.clear()


# Prozessweiter Stand (v1). Wie beim Audit (§6) können Tests einen eigenen Store
# injizieren; der Dienst nutzt diesen einen.
capacity_store = CapacityStore()


# --- Provider-Header → RateLimit -------------------------------------------------

_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_reset(value: str | None) -> float | None:
    """Reset-Angabe → Sekunden. Versteht `12`, `1.5s`, `6m0s`, `120ms`.

    OpenAI schreibt zusammengesetzte Dauern (`6m0s`), Anthropic einen ISO-Zeitpunkt,
    andere eine nackte Sekundenzahl. Was sich nicht sicher lesen lässt, ist `None` —
    eine falsch geratene Reset-Zeit sähe im Snapshot aus wie eine gemessene.
    """
    if not value:
        return None
    raw = value.strip()
    try:
        return float(raw)
    except ValueError:
        pass
    matches = _DURATION.findall(raw)
    if matches:
        return sum(float(amount) * _UNIT_SECONDS[unit] for amount, unit in matches)
    # ISO-8601-Zeitpunkt (Anthropic): Abstand zu jetzt, nie negativ.
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - datetime.now(UTC)).total_seconds())


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value.strip()))
    except (ValueError, AttributeError):
        return None


def rate_limit_from_headers(headers: Any, *, prefix: str) -> RateLimit | None:
    """Liest den Kopfstand aus Antwort-Headern (§7.6).

    Zwei Namensschemata sind im Umlauf:
    - `anthropic-ratelimit-{requests,tokens}-{limit,remaining,reset}`
    - `x-ratelimit-{limit,remaining,reset}-{requests,tokens}` (OpenAI, Mistral)

    `prefix` wählt das Schema (`"anthropic"` bzw. `"x"`). Meldet ein Provider gar
    nichts, ist das Ergebnis `None` — nicht ein RateLimit voller Nullen.
    """
    get = headers.get

    if prefix == "anthropic":
        limit = RateLimit(
            requests_limit=_int(get("anthropic-ratelimit-requests-limit")),
            requests_remaining=_int(get("anthropic-ratelimit-requests-remaining")),
            tokens_limit=_int(get("anthropic-ratelimit-tokens-limit")),
            tokens_remaining=_int(get("anthropic-ratelimit-tokens-remaining")),
            reset_seconds=parse_reset(
                get("anthropic-ratelimit-tokens-reset") or get("anthropic-ratelimit-requests-reset")
            ),
        )
    else:
        limit = RateLimit(
            requests_limit=_int(get("x-ratelimit-limit-requests")),
            requests_remaining=_int(get("x-ratelimit-remaining-requests")),
            tokens_limit=_int(get("x-ratelimit-limit-tokens")),
            tokens_remaining=_int(get("x-ratelimit-remaining-tokens")),
            reset_seconds=parse_reset(
                get("x-ratelimit-reset-tokens") or get("x-ratelimit-reset-requests")
            ),
        )

    if limit == RateLimit():
        return None
    return limit


def usage_from_payload(data: dict[str, Any]) -> Usage | None:
    """Liest den Verbrauch aus einer Provider-Antwort (§7.6).

    Deckt die drei v1-Dialekte ab: Anthropic (`usage.input_tokens`), OpenAI/Mistral
    (`usage.prompt_tokens`) und Gemini (`usageMetadata.promptTokenCount`). Fehlt der
    Block, ist der Verbrauch unbekannt (`None`) — nicht null.
    """
    block = data.get("usage")
    if isinstance(block, dict):
        if "input_tokens" in block or "output_tokens" in block:
            return Usage(
                input_tokens=int(block.get("input_tokens") or 0),
                output_tokens=int(block.get("output_tokens") or 0),
            )
        if "prompt_tokens" in block or "completion_tokens" in block:
            return Usage(
                input_tokens=int(block.get("prompt_tokens") or 0),
                output_tokens=int(block.get("completion_tokens") or 0),
            )
    meta = data.get("usageMetadata")
    if isinstance(meta, dict):
        return Usage(
            input_tokens=int(meta.get("promptTokenCount") or 0),
            output_tokens=int(meta.get("candidatesTokenCount") or 0),
        )
    return None


def usage_from_dict(block: Any) -> Usage | None:
    """Wire-Form → `Usage` (interner Gateway-Vertrag `/v1/complete`, §7.4).

    Gegenstück zu `Usage.as_dict`. `total_tokens` wird ignoriert — es ist abgeleitet,
    und ein Wert, der aus zwei Quellen kommen kann, läuft irgendwann auseinander.
    """
    if not isinstance(block, dict):
        return None
    return Usage(
        input_tokens=int(block.get("input_tokens") or 0),
        output_tokens=int(block.get("output_tokens") or 0),
    )


def rate_limit_from_dict(block: Any) -> RateLimit | None:
    """Wire-Form → `RateLimit` (interner Gateway-Vertrag, §7.4).

    `headroom` wird ignoriert — es ist abgeleitet (`RateLimit.headroom()`) und wird
    hier neu gerechnet statt übernommen.
    """
    if not isinstance(block, dict):
        return None
    limit = RateLimit(
        requests_limit=block.get("requests_limit"),
        requests_remaining=block.get("requests_remaining"),
        tokens_limit=block.get("tokens_limit"),
        tokens_remaining=block.get("tokens_remaining"),
        reset_seconds=block.get("reset_seconds"),
    )
    return None if limit == RateLimit() else limit


__all__ = [
    "CapacityRecord",
    "CapacityStore",
    "RateLimit",
    "StreamTelemetry",
    "Usage",
    "capacity_store",
    "parse_reset",
    "rate_limit_from_dict",
    "rate_limit_from_headers",
    "usage_from_dict",
    "usage_from_payload",
]
