"""Content-Flächen einer Message — was Modus und Verifier sehen müssen (Spec §5.5).

Der Proxy-Vertrag (§7.2) spricht von `messages`, aber `content` ist in der Praxis nicht
immer ein String. Anthropic und OpenAI schicken Block-Listen
(`[{"type": "text", "text": …}]`), ein Tool-Result trägt seinen Inhalt in `content`, und
ein Assistant-Turn trägt Tool-Argumente in `tool_calls`. Wer nur `isinstance(content, str)`
prüft, redigiert diese Flächen nicht und legt sie dem Verifier nie vor — die Anfrage geht
mit `released=true` raus, obwohl nur ein Teil geprüft wurde.

Das ist dieselbe Klasse wie die stille Kürzung im NER-Pfad (§5.3): **kein Ausfall, der
blockiert, sondern ein Durchlass, den niemand sieht.** Darum gilt hier dieselbe Regel —
was nicht als Textfläche aufzählbar ist, ist `opaque` und blockiert unter jedem Modus, der
den Verifier komponiert (§5.5). Nicht raten, nicht überspringen.

**Aufzählen und Ersetzen laufen über denselben Walker.** `surfaces()` sammelt nur,
`rebuild()` setzt in derselben Reihenfolge wieder ein — beide durchlaufen `_walk_message`.
Damit gilt strukturell, nicht bloß per Testfall: jede Fläche, die geprüft wird, ist auch
redigierbar und umgekehrt. Eine zweite, eigenständige Ersetz-Implementierung könnte
auseinanderlaufen; genau das ist der Fehler, den diese Datei schließt.

Drei Kategorien, weil nicht jede geprüfte Fläche auch beschreibbar ist:

- `texts`    — prüf- **und** redigierbar. Hier lebt der Inhalt.
- `readonly` — wird geprüft, aber nie überschrieben: die **Schlüssel** von Tool-Argument-
               Objekten. Sie zu redigieren könnte zwei Schlüssel auf denselben Platzhalter
               abbilden und ein Feld still verschlucken — eine stille Kürzung als „Fix"
               wäre schlimmer als das Loch. Steht ein Identifier in einem Schlüssel,
               blockiert stattdessen der Verifier (fail-closed statt stiller Korrektur).
- `opaque`   — gar nicht aufzählbar (Bilder, Audio, unbekannte Blocktypen). Blockiert
               unter jedem verifizierenden Modus.

Was bewusst **keine** Fläche ist: Protokoll-Identifikatoren (`role`, `tool_call_id`,
`tool_calls[].id`, `tool_calls[].name`). Das sind Vertragswerte, keine Nutzerinhalte — sie
zu redigieren zerbräche den Tool-Loop. Die vom Konsumenten *deklarierten* Tool-Specs
(Name/Beschreibung/Schema) sind eigener Egress und laufen über den Dispatch (§7.2), nicht
über diese Datei.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

# Blocktypen, deren `text`-Feld eine Textfläche ist — Anthropic (`text`) und die
# OpenAI-Responses-Dialekte (`input_text`/`output_text`).
TEXT_BLOCK_TYPES = frozenset({"text", "input_text", "output_text"})

# Blocktypen, die einen Tool-Aufruf mit Argument-Objekt tragen.
_TOOL_USE_TYPES = frozenset({"tool_use", "function_call"})
_TOOL_ARG_KEYS = ("input", "arguments")


class ContentShapeError(ValueError):
    """Programmierfehler beim Wiederaufbau (Anzahl der Ersetzungen passt nicht)."""


@dataclass(frozen=True)
class Surfaces:
    """Die aufgezählten Flächen einer oder mehrerer Messages (Reihenfolge = Walker-Ordnung)."""

    texts: tuple[str, ...] = ()
    readonly: tuple[str, ...] = ()
    opaque: tuple[str, ...] = ()

    @property
    def verifiable(self) -> tuple[str, ...]:
        """Alles, was der Verifier prüfen muss (§5) — beschreibbar oder nicht."""
        return (*self.texts, *self.readonly)

    def __bool__(self) -> bool:
        return bool(self.texts or self.readonly or self.opaque)

    def merge(self, other: Surfaces) -> Surfaces:
        return Surfaces(
            texts=(*self.texts, *other.texts),
            readonly=(*self.readonly, *other.readonly),
            opaque=(*self.opaque, *other.opaque),
        )


class _Acc:
    """Sammelstelle des Walkers. `repl` gesetzt ⇒ Wiederaufbau statt reiner Aufzählung."""

    def __init__(
        self, repl: Iterator[str] | None = None, *, read_only_texts: bool = False
    ) -> None:
        self.texts: list[str] = []
        self.readonly: list[str] = []
        self.opaque: list[str] = []
        self.repl = repl
        # Für Flächen, die geprüft, aber nie umgeschrieben werden (Tool-Specs, §7.2):
        # derselbe Walker, nur landet jeder String in `readonly` statt in `texts`.
        self._read_only_texts = read_only_texts

    def text(self, value: str) -> str:
        """Eine beschreibbare Textfläche — im Aufzähl-Lauf gesammelt, im Rebuild ersetzt."""
        if self._read_only_texts:
            return self.read_only(value)
        self.texts.append(value)
        if self.repl is None:
            return value
        try:
            return next(self.repl)
        except StopIteration:  # pragma: no cover — von rebuild() vorher abgefangen
            raise ContentShapeError("Zu wenige Ersetzungen für die Textflächen.") from None

    def read_only(self, value: str) -> str:
        self.readonly.append(value)
        return value

    def blind(self, what: str) -> None:
        self.opaque.append(what)

    def surfaces(self) -> Surfaces:
        return Surfaces(
            texts=tuple(self.texts),
            readonly=tuple(self.readonly),
            opaque=tuple(self.opaque),
        )


def _walk_json(node: Any, path: str, acc: _Acc) -> Any:
    """Ein JSON-Teilbaum (Tool-Argumente): jeder String ist Inhalt, jeder Schlüssel readonly."""
    if isinstance(node, str):
        return acc.text(node)
    if isinstance(node, dict):
        return {
            acc.read_only(k) if isinstance(k, str) else k: _walk_json(v, f"{path}.{k}", acc)
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_walk_json(v, f"{path}[{i}]", acc) for i, v in enumerate(node)]
    if node is None or isinstance(node, (bool, int, float)):
        return node  # Zahlen/Bools tragen keine Textfläche
    acc.blind(f"{path}: {type(node).__name__}")
    return node


def _walk_block(block: Any, path: str, acc: _Acc) -> Any:
    if isinstance(block, str):
        return acc.text(block)
    if not isinstance(block, dict):
        acc.blind(f"{path}: {type(block).__name__}")
        return block

    btype = block.get("type")
    out = dict(block)

    if btype in TEXT_BLOCK_TYPES:
        value = out.get("text")
        if isinstance(value, str):
            out["text"] = acc.text(value)
        else:
            acc.blind(f"{path}.type={btype!r} ohne text:str")
        return out

    if btype == "tool_result":
        # Anthropic: der Result-Inhalt ist str ODER eine Blockliste — dieselbe Mechanik.
        if "content" in out:
            out["content"] = _walk_content(out["content"], f"{path}.content", acc)
        else:
            acc.blind(f"{path}.type='tool_result' ohne content")
        return out

    if btype in _TOOL_USE_TYPES:
        key = next((k for k in _TOOL_ARG_KEYS if k in out), None)
        if key is None:
            acc.blind(f"{path}.type={btype!r} ohne {'/'.join(_TOOL_ARG_KEYS)}")
            return out
        out[key] = _walk_json(out[key], f"{path}.{key}", acc)
        return out

    acc.blind(f"{path}.type={btype!r}")
    return out


def _walk_content(node: Any, path: str, acc: _Acc) -> Any:
    if node is None:
        return None
    if isinstance(node, str):
        return acc.text(node)
    if isinstance(node, list):
        return [_walk_block(b, f"{path}[{i}]", acc) for i, b in enumerate(node)]
    acc.blind(f"{path}: {type(node).__name__}")
    return node


def _walk_tool_call(call: Any, path: str, acc: _Acc) -> Any:
    """Ein `tool_calls`-Eintrag einer Assistant-Message.

    Trägt die Argumente in beiden Dialekten: neutral als Objekt unter `arguments`
    (§7.2) oder OpenAI-typisch als JSON-**String** unter `function.arguments`. `id`/`name`
    sind Protokollwerte und bleiben unangetastet (siehe Modul-Docstring).
    """
    if not isinstance(call, dict):
        acc.blind(f"{path}: {type(call).__name__}")
        return call

    out = dict(call)
    fn = out.get("function")
    if isinstance(fn, dict) and "arguments" in fn:
        # OpenAI-Dialekt: `arguments` ist ein JSON-String. Er bleibt einer — die
        # typisierten Platzhalter (`[IP]`, `[EMAIL]`, …) enthalten weder " noch \,
        # die Ersetzung kann den String also nicht ungültig machen.
        args = fn["arguments"]
        if isinstance(args, str):
            out["function"] = {**fn, "arguments": acc.text(args)}
        else:
            out["function"] = {**fn, "arguments": _walk_json(args, f"{path}.function.arguments", acc)}
        return out
    if "arguments" in out:
        out["arguments"] = _walk_json(out["arguments"], f"{path}.arguments", acc)
        return out

    acc.blind(f"{path}: Tool-Call ohne arguments")
    return out


def _walk_message(message: Any, acc: _Acc) -> Any:
    if not isinstance(message, dict):
        acc.blind(f"message: {type(message).__name__}")
        return message

    out = dict(message)
    if "content" in out:
        out["content"] = _walk_content(out["content"], "content", acc)

    calls = out.get("tool_calls")
    if isinstance(calls, list):
        out["tool_calls"] = [
            _walk_tool_call(c, f"tool_calls[{i}]", acc) for i, c in enumerate(calls)
        ]
    elif calls is not None:
        acc.blind(f"tool_calls: {type(calls).__name__}")

    return out


def message_surfaces(message: Any) -> Surfaces:
    """Alle Flächen einer Message aufzählen — ohne sie zu verändern."""
    acc = _Acc()
    _walk_message(message, acc)
    return acc.surfaces()


def messages_surfaces(messages: Sequence[Any]) -> Surfaces:
    """Alle Flächen einer Message-Liste, in Reihenfolge."""
    acc = _Acc()
    for m in messages:
        _walk_message(m, acc)
    return acc.surfaces()


def tool_surfaces(tools: Sequence[Any]) -> Surfaces:
    """Die Flächen der **deklarierten** Tool-Specs (§7.2) — geprüft, nie umgeschrieben.

    Name, Beschreibung und jedes Schema-Feld sind ausgehender Inhalt und gehören in die
    verifizierte Fläche (§5). Umgeschrieben werden sie aber nicht: ein redigierter
    Tool-**Name** passt zu keinem deklarierten Tool mehr, und ein Schema mit Platzhaltern
    im Typ ist kaputt. Steht ein Identifier in einer Spec, ist das ein Fehler des
    Konsumenten — dann blockiert der Verifier, statt still etwas zurechtzubiegen.

    Läuft über denselben Walker wie alles andere: gleiche Abdeckung, gleiche
    Fail-closed-Regel für nicht aufzählbare Teile.
    """
    acc = _Acc(read_only_texts=True)
    for i, tool in enumerate(tools):
        _walk_json(tool, f"tools[{i}]", acc)
    return acc.surfaces()


def rebuild_message(message: Any, texts: Sequence[str]) -> Any:
    """Message mit ersetzten Textflächen — `texts` in der Reihenfolge von `message_surfaces`.

    Fail-closed gegen Programmierfehler: passt die Anzahl nicht, ist das ein Bruch zwischen
    Aufzählung und Ersetzung und wird als `ContentShapeError` geworfen, nicht stillschweigend
    zurechtgebogen (eine falsch ausgerichtete Ersetzung wäre eine Datenverfälschung).
    """
    acc = _Acc(iter(texts))
    out = _walk_message(message, acc)
    if len(acc.texts) != len(texts):
        raise ContentShapeError(
            f"{len(texts)} Ersetzungen für {len(acc.texts)} Textflächen — "
            f"Aufzählung und Ersetzung sind auseinandergelaufen."
        )
    return out
