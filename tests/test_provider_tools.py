"""Tool-Calling in den Adaptern (§7.3, Rev. 15).

Jeder v1-Adapter übersetzt die **neutrale** Form in sein natives Schema und die native
Antwort zurück. Geprüft wird beides je Provider: was auf die Leitung geht (der Body) und
was als `ProviderResponse` zurückkommt.

Der Anthropic-Adapter hat seine Fälle in `test_tool_calling.py`; hier stehen die drei, die
Rev. 15 dazugenommen hat. Kein Netz: httpx.MockTransport.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from sluice.providers import ProviderError, ToolCall
from sluice.providers.gemini import GeminiAdapter
from sluice.providers.mistral import MistralAdapter
from sluice.providers.openai import OpenAIAdapter

TOOLS = [
    {
        "name": "get_weather",
        "description": "Wetter einer Stadt.",
        "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
    }
]

# Ein vollständiger Tool-Turn in der neutralen Form, wie der Kern ihn liefert.
TOOL_TURN = [
    {"role": "system", "content": "knapp"},
    {"role": "user", "content": "Wetter in Berlin?"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "t1", "name": "get_weather", "arguments": {"city": "Berlin"}}],
    },
    {"role": "tool", "tool_call_id": "t1", "content": "18C"},
]


def _capture(response: dict[str, Any]) -> tuple[dict[str, Any], httpx.MockTransport]:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=response)

    return seen, httpx.MockTransport(handler)


# ---------- OpenAI / Mistral (gemeinsamer Dialekt) ----------

OPENAI_REPLY = {
    "model": "gpt-x",
    "choices": [
        {
            "finish_reason": "tool_calls",
            "message": {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "Berlin"}',
                        },
                    }
                ],
            },
        }
    ],
}


@pytest.mark.parametrize("adapter_cls", [OpenAIAdapter, MistralAdapter])
async def test_openai_family_renders_tools_natively(adapter_cls: type) -> None:
    seen, transport = _capture(OPENAI_REPLY)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = adapter_cls(api_key="k", http_client=client)
        resp = await adapter.complete(TOOL_TURN, model="gpt-x", tools=TOOLS)

    # Neutrale Spec → OpenAI-Schema (`type`/`function`/`parameters`).
    assert seen["body"]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Wetter einer Stadt.",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]
    # Neutrale tool_calls → geschachtelt, `arguments` als JSON-String.
    assistant = seen["body"]["messages"][2]
    assert assistant["tool_calls"] == [
        {
            "id": "t1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Berlin"}'},
        }
    ]
    # Das Tool-Result trägt bereits die native Form und bleibt unangetastet.
    assert seen["body"]["messages"][3] == {
        "role": "tool",
        "tool_call_id": "t1",
        "content": "18C",
    }
    # Rückweg: native tool_calls → neutral, `arguments` wieder als Objekt.
    assert resp.tool_calls == (
        ToolCall(id="call_1", name="get_weather", arguments={"city": "Berlin"}),
    )
    assert resp.stop_reason == "tool_calls"


async def test_openai_family_plain_messages_are_unchanged() -> None:
    # Ohne Tools bleibt der Body byte-gleich zu vorher.
    seen, transport = _capture({"model": "m", "choices": [{"message": {"content": "hi"}}]})
    plain = [{"role": "user", "content": "hallo"}]
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await OpenAIAdapter(api_key="k", http_client=client).complete(plain, model="m")
    assert seen["body"]["messages"] == plain
    assert "tools" not in seen["body"]
    assert resp.text == "hi"
    assert resp.tool_calls == ()


async def test_openai_family_reports_unparsable_arguments() -> None:
    # Ein kaputter arguments-String ist ein Vertragsbruch des Providers — melden,
    # nicht als leeres Argument-Objekt beschönigen (das Tool liefe sonst auf nichts).
    broken = {
        "model": "m",
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "c", "type": "function", "function": {"name": "n", "arguments": "{"}}
                    ],
                }
            }
        ],
    }
    _, transport = _capture(broken)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ProviderError):
            await OpenAIAdapter(api_key="k", http_client=client).complete(
                [{"role": "user", "content": "x"}], model="m"
            )


# ---------- Gemini (echte Übersetzung: functionCall / functionResponse) ----------

GEMINI_REPLY = {
    "candidates": [
        {
            "finishReason": "STOP",
            "content": {
                "parts": [
                    {"text": "Ich frage das Tool."},
                    {"functionCall": {"name": "get_weather", "args": {"city": "Berlin"}}},
                ]
            },
        }
    ]
}


async def test_gemini_renders_function_declarations_and_tool_turn() -> None:
    seen, transport = _capture(GEMINI_REPLY)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = GeminiAdapter(api_key="k", http_client=client)
        resp = await adapter.complete(TOOL_TURN, model="gemini-x", tools=TOOLS)

    body = seen["body"]
    assert body["systemInstruction"] == {"parts": [{"text": "knapp"}]}
    assert body["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "get_weather",
                    "description": "Wetter einer Stadt.",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                }
            ]
        }
    ]
    contents = body["contents"]
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    # assistant.tool_calls → functionCall-Part (leerer content erzeugt KEINEN Text-Part).
    assert contents[1]["parts"] == [
        {"functionCall": {"name": "get_weather", "args": {"city": "Berlin"}}}
    ]
    # role:"tool" → functionResponse; der Name kommt aus dem vorangegangenen Call.
    assert contents[2]["parts"] == [
        {"functionResponse": {"name": "get_weather", "response": {"result": "18C"}}}
    ]

    # Rückweg: functionCall → neutraler ToolCall mit synthetischer, stabiler ID.
    assert resp.text == "Ich frage das Tool."
    assert resp.tool_calls == (
        ToolCall(id="get_weather-1", name="get_weather", arguments={"city": "Berlin"}),
    )
    assert resp.stop_reason == "STOP"


async def test_gemini_round_trip_resolves_the_synthetic_id() -> None:
    # Die von Sluice erzeugte ID muss im Folge-Turn wieder zum Namen auflösbar sein —
    # sonst wäre sie eine Sackgasse und der Loop bräche nach der ersten Runde ab.
    seen, transport = _capture(GEMINI_REPLY)
    follow_up = [
        {"role": "user", "content": "Wetter?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "get_weather-1", "name": "get_weather", "arguments": {"city": "Berlin"}}
            ],
        },
        {"role": "tool", "tool_call_id": "get_weather-1", "content": "18C"},
    ]
    async with httpx.AsyncClient(transport=transport) as client:
        await GeminiAdapter(api_key="k", http_client=client).complete(follow_up, model="m")
    assert seen["body"]["contents"][2]["parts"][0]["functionResponse"]["name"] == "get_weather"


async def test_gemini_merges_consecutive_tool_results() -> None:
    seen, transport = _capture(GEMINI_REPLY)
    convo = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "a", "name": "eins", "arguments": {}},
                {"id": "b", "name": "zwei", "arguments": {}},
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "ra"},
        {"role": "tool", "tool_call_id": "b", "content": "rb"},
    ]
    async with httpx.AsyncClient(transport=transport) as client:
        await GeminiAdapter(api_key="k", http_client=client).complete(convo, model="m")
    contents = seen["body"]["contents"]
    assert [c["role"] for c in contents] == ["model", "user"]  # zwei Results → EIN Content
    assert len(contents[1]["parts"]) == 2


async def test_gemini_fails_closed_on_unresolvable_tool_call_id() -> None:
    # Ohne auflösbaren Namen wird NICHT geraten — ein falsch zugeordnetes Tool-Result
    # wäre schlimmer als ein Fehler.
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("darf nie gesendet werden")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="tool_call_id"):
            await GeminiAdapter(api_key="k", http_client=client).complete(
                [{"role": "tool", "tool_call_id": "unbekannt", "content": "r"}], model="m"
            )


async def test_gemini_plain_messages_are_unchanged() -> None:
    seen, transport = _capture({"candidates": [{"content": {"parts": [{"text": "hi"}]}}]})
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await GeminiAdapter(api_key="k", http_client=client).complete(
            [{"role": "user", "content": "hallo"}], model="m"
        )
    assert seen["body"]["contents"] == [{"role": "user", "parts": [{"text": "hallo"}]}]
    assert "tools" not in seen["body"]
    assert resp.text == "hi"
    assert resp.tool_calls == ()
