"""Dialekt-Grenze (§7.2, Rev. 15).

Der Kern ist neutral; Konsumenten sprechen überwiegend den OpenAI-Dialekt. Die
Übersetzung sitzt allein im Endpoint, läuft **vor** dem Guard (damit Tool-Argumente als
aufgelöste Werte geprüft und redigiert werden) und ist symmetrisch: die Antwort kommt in
dem Dialekt, in dem die Anfrage kam.

Kein Netz: Adapter injiziert.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from sluice.audit import AuditLog
from sluice.dialect import (
    NEUTRAL,
    OPENAI,
    DialectError,
    check_tool_choice,
    detect_tool_dialect,
    normalize_messages,
    normalize_tools,
    render_tool_calls,
)
from sluice.policy import Profile
from sluice.providers import ProviderResponse, ToolCall
from sluice.server import create_app

NEUTRAL_TOOLS = [
    {
        "name": "get_weather",
        "description": "Wetter einer Stadt.",
        "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
    }
]

OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Wetter einer Stadt.",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }
]

PASSTHROUGH = Profile(
    name="prismclaw",
    mode="passthrough",
    egress_enabled=True,
    allowed_purposes=("external_escalation",),
    provider_allowlist=("claude",),
    detector_profile="infra",
    allowed_modes=("passthrough",),
)

STRICT = Profile(
    name="openclaw",
    mode="strict",
    egress_enabled=True,
    allowed_purposes=("external_escalation",),
    provider_allowlist=("claude",),
    detector_profile="infra",
)


class RecordingAdapter:
    name = "fake"

    def __init__(self, tool_calls: tuple[ToolCall, ...] = ()) -> None:
        self._tool_calls = tool_calls
        self.calls: list[list[dict[str, Any]]] = []
        self.tools_seen: list[dict[str, Any]] | None = None

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int = 1024,
        tools: list[dict[str, Any]] | None = None,
    ) -> ProviderResponse:
        self.calls.append(messages)
        self.tools_seen = tools
        return ProviderResponse(
            text="ok", model=model, provider=self.name, tool_calls=self._tool_calls
        )

    async def stream(
        self, messages: list[dict[str, Any]], *, model: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        self.calls.append(messages)
        for chunk in ():
            yield chunk


def _client(adapter: RecordingAdapter) -> httpx.AsyncClient:
    app = create_app(
        {"prismclaw": PASSTHROUGH, "openclaw": STRICT},
        adapter_factory=lambda name: adapter,
        audit=AuditLog(),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://sluice"
    )


# ---------- Erkennung: die Anfrage sagt selbst, welche Form sie versteht ----------


def test_detects_openai_tools() -> None:
    assert detect_tool_dialect(OPENAI_TOOLS) == OPENAI


def test_detects_neutral_tools() -> None:
    assert detect_tool_dialect(NEUTRAL_TOOLS) == NEUTRAL


def test_detects_openai_from_messages_when_no_tools_declared() -> None:
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "t", "type": "function", "function": {"name": "n", "arguments": "{}"}}
            ],
        }
    ]
    assert detect_tool_dialect(None, messages) == OPENAI


def test_defaults_to_neutral() -> None:
    assert detect_tool_dialect(None, [{"role": "user", "content": "x"}]) == NEUTRAL


# ---------- Specs: beide Formen rein, neutrale raus ----------


def test_openai_spec_becomes_neutral() -> None:
    assert normalize_tools(OPENAI_TOOLS) == NEUTRAL_TOOLS


def test_neutral_spec_is_idempotent() -> None:
    assert normalize_tools(NEUTRAL_TOOLS) == NEUTRAL_TOOLS


def test_spec_without_name_is_rejected() -> None:
    # Ohne Namen ließe sich die Spec weder verifizieren noch einem Call zuordnen.
    with pytest.raises(DialectError):
        normalize_tools([{"description": "namenlos"}])


def test_tools_must_be_a_list() -> None:
    with pytest.raises(DialectError):
        normalize_tools({"name": "x"})


# ---------- Tool-Calls in Messages: JSON-String → Objekt (vor dem Guard) ----------


def test_openai_tool_call_arguments_are_parsed() -> None:
    out = normalize_messages(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "t1",
                        "type": "function",
                        "function": {"name": "ssh", "arguments": '{"host": "example.org"}'},
                    }
                ],
            }
        ]
    )
    assert out[0]["tool_calls"] == [
        {"id": "t1", "name": "ssh", "arguments": {"host": "example.org"}}
    ]


def test_neutral_tool_calls_pass_through() -> None:
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "t", "name": "n", "arguments": {"a": "b"}}]}
    ]
    assert normalize_messages(messages) == messages


def test_plain_messages_are_untouched() -> None:
    messages = [{"role": "user", "content": "hallo"}]
    assert normalize_messages(messages) == messages


def test_broken_arguments_json_is_rejected() -> None:
    with pytest.raises(DialectError):
        normalize_messages(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "t", "type": "function", "function": {"name": "n", "arguments": "{"}}
                    ],
                }
            ]
        )


def test_non_object_arguments_are_rejected() -> None:
    with pytest.raises(DialectError):
        normalize_messages(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "t", "type": "function", "function": {"name": "n", "arguments": "[]"}}
                    ],
                }
            ]
        )


# ---------- tool_choice: durchlassen oder abweisen, nie ignorieren ----------


@pytest.mark.parametrize("choice", [None, "auto", "none"])
def test_permissive_tool_choice_passes(choice: object) -> None:
    check_tool_choice(choice)


@pytest.mark.parametrize(
    "choice", ["required", {"type": "function", "function": {"name": "ssh"}}]
)
def test_forced_tool_choice_is_rejected(choice: object) -> None:
    # Verschlucken hieße: eine Zusage geben, die die Boundary nicht einlöst.
    with pytest.raises(DialectError):
        check_tool_choice(choice)


# ---------- Rendering: die Antwort spricht den Dialekt der Anfrage ----------


def test_render_neutral_is_unchanged() -> None:
    calls = [{"id": "t", "name": "n", "arguments": {"a": "b"}}]
    assert render_tool_calls(calls, NEUTRAL) == calls


def test_render_openai_nests_and_stringifies_arguments() -> None:
    rendered = render_tool_calls([{"id": "t", "name": "n", "arguments": {"a": "b"}}], OPENAI)
    assert rendered == [
        {"id": "t", "type": "function", "function": {"name": "n", "arguments": '{"a": "b"}'}}
    ]
    # Der String muss wieder parsebar sein — der Konsument führt darauf sein Tool aus.
    assert json.loads(rendered[0]["function"]["arguments"]) == {"a": "b"}


# ---------- Endpoint: rein und raus im selben Dialekt ----------


async def test_openai_request_gets_openai_response() -> None:
    adapter = RecordingAdapter(
        tool_calls=(ToolCall(id="t1", name="get_weather", arguments={"city": "Berlin"}),)
    )
    client = _client(adapter)
    resp = await client.post(
        "/v1/chat/completions",
        headers={"X-Sluice-Profile": "prismclaw"},
        json={
            "messages": [{"role": "user", "content": "Wetter?"}],
            "model": "claude-sonnet-5",
            "tools": OPENAI_TOOLS,
            "tool_choice": "auto",
        },
    )
    assert resp.status_code == 200
    # Der Adapter hat die NEUTRALE Spec gesehen — der Dialekt endet am Endpoint.
    assert adapter.tools_seen == NEUTRAL_TOOLS
    choice = resp.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"] == [
        {
            "id": "t1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Berlin"}'},
        }
    ]
    await client.aclose()


async def test_neutral_request_still_gets_neutral_response() -> None:
    adapter = RecordingAdapter(
        tool_calls=(ToolCall(id="t1", name="get_weather", arguments={"city": "Berlin"}),)
    )
    client = _client(adapter)
    resp = await client.post(
        "/v1/chat/completions",
        headers={"X-Sluice-Profile": "prismclaw"},
        json={
            "messages": [{"role": "user", "content": "Wetter?"}],
            "model": "m",
            "tools": NEUTRAL_TOOLS,
        },
    )
    assert resp.json()["choices"][0]["message"]["tool_calls"] == [
        {"id": "t1", "name": "get_weather", "arguments": {"city": "Berlin"}}
    ]
    await client.aclose()


async def test_openai_tool_turn_is_redacted_under_strict() -> None:
    # Der agentische Folge-Turn eines OpenAI-Konsumenten: Tool-Args als JSON-String,
    # Tool-Result als Message. Unter `strict` muss beides redigiert beim Adapter ankommen.
    adapter = RecordingAdapter()
    client = _client(adapter)
    resp = await client.post(
        "/v1/chat/completions",
        headers={"X-Sluice-Profile": "openclaw"},
        json={
            "model": "m",
            "tools": OPENAI_TOOLS,
            "messages": [
                {"role": "user", "content": "Status?"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "t1",
                            "type": "function",
                            "function": {"name": "ssh", "arguments": '{"host": "192.168.1.10"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "t1", "content": "erreichbar, mail a@b.de"},
            ],
        },
    )
    assert resp.status_code == 200
    sent = adapter.calls[0]
    assert sent[1]["tool_calls"][0]["arguments"] == {"host": "[IP]"}
    assert sent[2]["content"] == "erreichbar, mail [EMAIL]"
    await client.aclose()


async def test_forced_tool_choice_is_rejected_at_the_endpoint() -> None:
    adapter = RecordingAdapter()
    client = _client(adapter)
    resp = await client.post(
        "/v1/chat/completions",
        headers={"X-Sluice-Profile": "prismclaw"},
        json={
            "messages": [{"role": "user", "content": "x"}],
            "model": "m",
            "tools": OPENAI_TOOLS,
            "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
        },
    )
    assert resp.status_code == 400
    assert adapter.calls == []
    await client.aclose()


async def test_stream_with_tools_still_blocks() -> None:
    adapter = RecordingAdapter()
    client = _client(adapter)
    resp = await client.post(
        "/v1/chat/completions",
        headers={"X-Sluice-Profile": "prismclaw"},
        json={
            "messages": [{"role": "user", "content": "x"}],
            "model": "m",
            "tools": OPENAI_TOOLS,
            "stream": True,
        },
    )
    assert resp.status_code == 403
    assert adapter.calls == []
    await client.aclose()
