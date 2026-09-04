"""Dialekt-Übersetzung an der Konsumenten-Schnittstelle (Spec §7.2, Rev. 15).

Der Kern ist **neutral**: Tool-Specs `{name, description, input_schema}`, Tool-Calls
`{id, name, arguments}` mit `arguments` als **Objekt**. Konsumenten sprechen aber
überwiegend den OpenAI-Dialekt — der Endpoint heißt schließlich `/v1/chat/completions`,
und wer ihn als „OpenAI-kompatiblen Provider" einträgt, schickt
`{"type":"function","function":{…}}` und erwartet `tool_calls` in derselben Form zurück.
Ohne Übersetzung ist Sluice für genau die Konsumenten unerreichbar, für die es gebaut ist.

Diese Datei ist die **einzige** Stelle im Code, die die Abbildung neutral↔OpenAI kennt.
Alles zwischen Endpoint und Adapter — Guard, Modi, Verifier, Audit — sieht ausschließlich
die neutrale Form. Der Übersetzungspunkt am Endpoint liegt bewusst **vor** dem Guard: der
Guard soll die Argumente als aufgelöste Werte prüfen und redigieren, nicht als
undurchsichtigen JSON-String.

**Zwei Nutzer, eine Abbildung.** Der OpenAI-Dialekt ist nicht nur eine Konsumenten-Form,
er ist auch das *native* Schema zweier Provider (OpenAI, Mistral). Der Endpoint übersetzt
damit nach außen zum Konsumenten, der `openai_compat`-Adapter (§7.3) nach außen zum
Provider — dieselben Funktionen. Zwei getrennte Implementierungen derselben Abbildung
würden auseinanderlaufen, und ein Fehler darin fiele als vertauschtes Tool-Argument auf,
nicht als Absturz.

**Symmetrie statt Schalter.** Der Dialekt der Antwort ist der Dialekt der Anfrage. Wer
OpenAI-Specs schickt, bekommt OpenAI-`tool_calls`; wer neutrale schickt, neutrale. Kein
zusätzliches Feld, das man vergessen kann, und keine Vermutung über den Aufrufer — die
Anfrage sagt selbst, welche Form sie versteht.

**Fail-closed.** Eine Form, die weder neutral noch OpenAI ist, ein `arguments`-String, der
kein JSON-Objekt ergibt, oder ein `tool_choice`, das mehr verlangt als „darfst du" — all
das wird abgewiesen (`DialectError`), nie geraten und nie stillschweigend fallen gelassen.
Ein falsch geratenes Tool-Schema wäre ungeprüfter Egress; ein verworfenes `tool_choice`
wäre ein Vertrag, den Sluice zusagt und nicht einhält.
"""

from __future__ import annotations

import json
from typing import Any

NEUTRAL = "neutral"
OPENAI = "openai"

# `tool_choice`-Werte, die nichts erzwingen und daher folgenlos durchgehen können.
_PERMISSIVE_TOOL_CHOICE = frozenset({"auto", "none"})


class DialectError(ValueError):
    """Nicht übersetzbare Tool-Form — abweisen, nicht raten (§7.2)."""


def _is_openai_tool(tool: Any) -> bool:
    return isinstance(tool, dict) and isinstance(tool.get("function"), dict)


def detect_tool_dialect(
    tools: list[Any] | None, messages: list[Any] | None = None
) -> str:
    """Welchen Dialekt spricht diese Anfrage? Die Antwort bekommt denselben zurück.

    Erkennungsmerkmal ist die *Struktur*, nicht ein Flag: der OpenAI-Dialekt schachtelt
    Name und Parameter unter `function`, die neutrale Form nicht. Findet sich in Specs
    und Messages kein Hinweis, bleibt es bei `neutral` (der Vertragsform, §7.2).
    """
    for tool in tools or ():
        if _is_openai_tool(tool):
            return OPENAI
    for message in messages or ():
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or ():
            if _is_openai_tool(call):
                return OPENAI
    return NEUTRAL


def normalize_tools(tools: list[Any] | None) -> list[dict[str, Any]] | None:
    """Tool-Specs → neutrale Form `{name, description, input_schema}`.

    Beide Dialekte werden angenommen; alles andere ist ein Fehler. Insbesondere wird eine
    Spec **ohne** `name` abgewiesen: sie ließe sich weder verifizieren noch einem
    zurückkommenden Tool-Call zuordnen.
    """
    if tools is None:
        return None
    if not isinstance(tools, list):
        raise DialectError("`tools` muss eine Liste sein.")

    out: list[dict[str, Any]] = []
    for i, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise DialectError(f"tools[{i}]: Objekt erwartet, {type(tool).__name__} bekommen.")
        if _is_openai_tool(tool):
            fn = tool["function"]
            spec = {
                "name": fn.get("name"),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        else:
            spec = {
                "name": tool.get("name"),
                "description": tool.get("description", ""),
                "input_schema": tool.get("input_schema")
                or tool.get("parameters")
                or {"type": "object", "properties": {}},
            }
        if not isinstance(spec["name"], str) or not spec["name"]:
            raise DialectError(f"tools[{i}]: Tool-Spec ohne verwertbaren `name`.")
        out.append(spec)
    return out


def _normalize_arguments(value: Any, where: str) -> dict[str, Any]:
    """`arguments` → Objekt. Der OpenAI-Dialekt liefert einen JSON-String.

    Der String wird hier aufgelöst, damit der Guard die einzelnen Werte sieht und
    redigieren kann (§5.5) — ein Argument-Blob, den niemand aufmacht, wäre ungeprüfter
    Egress in einem Feld, das per Definition Nutzerdaten trägt.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except json.JSONDecodeError as exc:
            raise DialectError(f"{where}: `arguments` ist kein gültiges JSON ({exc}).") from None
        if not isinstance(parsed, dict):
            raise DialectError(f"{where}: `arguments` muss ein JSON-Objekt sein.")
        return parsed
    raise DialectError(f"{where}: `arguments` muss Objekt oder JSON-String sein.")


def normalize_tool_calls(calls: Any, where: str) -> list[dict[str, Any]]:
    """Tool-Calls beider Dialekte → neutrale Form `{id, name, arguments}`.

    Auch der Rückweg braucht das: eine OpenAI-*Antwort* trägt dieselbe geschachtelte Form
    wie eine OpenAI-Anfrage, der `openai_compat`-Adapter liest sie über genau diese
    Funktion (§7.3).
    """
    if not isinstance(calls, list):
        raise DialectError(f"{where}: `tool_calls` muss eine Liste sein.")
    out: list[dict[str, Any]] = []
    for j, call in enumerate(calls):
        at = f"{where}[{j}]"
        if not isinstance(call, dict):
            raise DialectError(f"{at}: Objekt erwartet.")
        if _is_openai_tool(call):
            fn = call["function"]
            out.append(
                {
                    "id": call.get("id", ""),
                    "name": fn.get("name", ""),
                    "arguments": _normalize_arguments(fn.get("arguments"), at),
                }
            )
        else:
            out.append(
                {
                    "id": call.get("id", ""),
                    "name": call.get("name", ""),
                    "arguments": _normalize_arguments(call.get("arguments", {}), at),
                }
            )
    return out


def normalize_messages(messages: list[Any]) -> list[Any]:
    """Messages → neutrale Form; `tool_calls` werden aufgelöst, alles andere bleibt.

    Idempotent: neutrale Messages laufen unverändert durch. Nicht-Tool-Messages werden
    nicht angefasst — der Nicht-Tool-Pfad bleibt byte-gleich.
    """
    out: list[Any] = []
    for i, message in enumerate(messages):
        calls = message.get("tool_calls") if isinstance(message, dict) else None
        if not calls:
            out.append(message)
            continue
        normalized = normalize_tool_calls(calls, f"messages[{i}].tool_calls")
        out.append({**message, "tool_calls": normalized})
    return out


def to_openai_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Neutrale Tool-Specs → OpenAI-Schema (`type`/`function`/`parameters`)."""
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for t in tools
    ]


def to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Neutrale Messages → OpenAI-Schema; nur `tool_calls` werden umgeschrieben.

    `role:"tool"`-Results tragen bereits die OpenAI-Form (`tool_call_id` + `content`) und
    bleiben unangetastet. Reine Text-Messages ebenfalls — der Nicht-Tool-Pfad bleibt
    byte-gleich zu vorher.
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        calls = message.get("tool_calls") if isinstance(message, dict) else None
        if not calls:
            out.append(message)
            continue
        out.append({**message, "tool_calls": render_tool_calls(calls, OPENAI)})
    return out


def messages_carry_tool_artifacts(messages: list[Any] | None) -> bool:
    """Tragen die Messages einen laufenden Tool-Turn (Calls, Results, Tool-Blöcke)?

    Ein agentischer Folge-Turn kann `tools` weglassen und trotzdem `tool_calls` und
    `role:"tool"`-Results mitschicken. Der Provider muss die Form dann genauso verstehen
    wie beim ersten Turn — sonst geht eine Konversation raus, die er nicht lesen kann.
    Deshalb prüft der Dispatch die Adapter-Fähigkeit auch für diesen Fall (§7.2).
    """
    for message in messages or ():
        if not isinstance(message, dict):
            continue
        if message.get("tool_calls") or message.get("role") == "tool":
            return True
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result", "function_call")
            for b in content
        ):
            return True
    return False


def check_tool_choice(tool_choice: Any) -> None:
    """`tool_choice` durchlassen oder abweisen — nie ignorieren.

    Sluice reicht `tool_choice` (noch) nicht an die Adapter weiter. Ein Wert, der nichts
    erzwingt (`auto`/`none`, oder gar keiner), ist folgenlos und geht durch. Ein Wert, der
    ein bestimmtes Tool **verlangt**, wird abgewiesen: ihn zu verschlucken hieße, dem
    Konsumenten eine Zusage zu geben, die die Boundary nicht einlöst — der Loop wartete
    auf einen erzwungenen Tool-Call, der nie kommt.
    """
    if tool_choice is None:
        return
    if isinstance(tool_choice, str) and tool_choice in _PERMISSIVE_TOOL_CHOICE:
        return
    raise DialectError(
        "`tool_choice` mit erzwungenem Tool wird nicht unterstützt (nur 'auto'/'none') — "
        "fail-closed statt stillem Ignorieren (§7.2)."
    )


def render_tool_calls(
    tool_calls: list[dict[str, Any]] | None, dialect: str
) -> list[dict[str, Any]] | None:
    """Neutrale Tool-Calls → der Dialekt, in dem die Anfrage kam."""
    if not tool_calls:
        return None
    if dialect != OPENAI:
        return tool_calls
    return [
        {
            "id": call["id"],
            "type": "function",
            "function": {
                "name": call["name"],
                # OpenAI erwartet `arguments` als JSON-**String**, nicht als Objekt.
                "arguments": json.dumps(call["arguments"], ensure_ascii=False),
            },
        }
        for call in tool_calls
    ]
