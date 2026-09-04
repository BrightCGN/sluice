"""Tool-Calling an der Boundary (§7.2, Rev. 14/15).

Tools stehen **im Payload**, nicht daneben — es gibt keinen Weg, sie zu senden, ohne dass
der Guard sie sieht. Er verifiziert die Specs als `readonly`-Fläche (geprüft, nie
umgeschrieben), deshalb ist Tool-Calling seit Rev. 15 unter **jedem** Modus möglich und
nicht mehr nur unter `passthrough`. Fail-closed bleibt, wo Sluice eine Zusage nicht
einlösen kann: nicht-tool-fähiger Provider, Streaming, erzwungenes `tool_choice`.

Ein tool-fähiger Adapter (`anthropic`) rendert die neutrale Spec nativ und gibt
`tool_calls` zurück; der Endpoint liefert sie in dem Dialekt, in dem die Anfrage kam.

Kein Netz: der Adapter wird injiziert bzw. via httpx.MockTransport gestellt.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from sluice.audit import AuditLog
from sluice.dispatch import guarded_completion
from sluice.modes import EgressPayload
from sluice.policy import Profile, ReversibleConfig
from sluice.providers import ProviderResponse, ToolCall
from sluice.providers.anthropic import AnthropicAdapter
from sluice.server import create_app

TOOLS = [
    {
        "name": "get_weather",
        "description": "Aktuelles Wetter einer Stadt.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
]

PASSTHROUGH = Profile(
    name="prismclaw",
    mode="passthrough",
    egress_enabled=True,
    allowed_purposes=("external_escalation",),
    provider_allowlist=("claude", "gemini"),
    detector_profile="infra",
    allowed_modes=("passthrough",),  # Rev. 11: fail-open-Modus braucht explizites Opt-in
)

REVERSIBLE = Profile(
    name="aider",
    mode="pseudonymizing",
    egress_enabled=True,
    allowed_purposes=("external_escalation",),
    provider_allowlist=("claude",),
    detector_profile="infra",
    reversible=ReversibleConfig(),  # leere allowed_modes erlaubt verifizierende Modi (§4.1)
)

VERIFYING = Profile(
    name="temper",
    mode="generalizing",
    egress_enabled=True,
    allowed_purposes=("external_escalation",),
    provider_allowlist=("claude",),
    detector_profile="infra",
)


class ToolFakeAdapter:
    """Zeichnet auf, ob (und mit welchen) `tools` er erreicht wurde, und kann
    `tool_calls` zurückgeben — ohne echten Provider."""

    name = "fake"

    def __init__(
        self, *, reply: str = "ok", tool_calls: tuple[ToolCall, ...] = ()
    ) -> None:
        self.reply = reply
        self._tool_calls = tuple(tool_calls)
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
            text=self.reply,
            model=model,
            provider=self.name,
            tool_calls=self._tool_calls,
            stop_reason="tool_use" if self._tool_calls else "end",
        )

    async def stream(
        self, messages: list[dict[str, Any]], *, model: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        self.calls.append(messages)
        for chunk in ():  # kein Streaming in diesem Slice
            yield chunk


# ---------- Dispatch: passthrough trägt tools durch, tool_calls kommen zurück ----------


async def test_passthrough_forwards_tools_and_returns_tool_calls() -> None:
    adapter = ToolFakeAdapter(
        tool_calls=(ToolCall(id="t1", name="get_weather", arguments={"city": "Berlin"}),)
    )
    outcome = await guarded_completion(
        profile=PASSTHROUGH,
        purpose="external_escalation",
        payload=EgressPayload(
            raw_text="Wetter in Berlin?",
            messages=[{"role": "user", "content": "Wetter in Berlin?"}],
            tools=TOOLS,
        ),
        provider_target="claude",
        model="claude-sonnet-5",
        adapter=adapter,
        audit=AuditLog(),
    )
    assert outcome.released is True
    assert adapter.tools_seen == TOOLS  # tools haben den Adapter erreicht
    assert outcome.finish_reason == "tool_calls"
    assert outcome.tool_calls == [
        {"id": "t1", "name": "get_weather", "arguments": {"city": "Berlin"}}
    ]


async def test_no_tools_behaviour_unchanged() -> None:
    # Ohne `tools` ist das Verhalten byte-gleich zu Rev. 13.
    adapter = ToolFakeAdapter(reply="Antwort")
    outcome = await guarded_completion(
        profile=PASSTHROUGH,
        purpose="external_escalation",
        payload=EgressPayload(
            raw_text="hallo", messages=[{"role": "user", "content": "hallo"}]
        ),
        provider_target="claude",
        model="m",
        adapter=adapter,
        audit=AuditLog(),
    )
    assert outcome.released is True
    assert adapter.tools_seen is None
    assert outcome.tool_calls is None
    assert outcome.finish_reason == "stop"


# ---------- Fail-closed: kein stilles Loch ----------


async def test_verifying_mode_allows_clean_tools() -> None:
    # Rev. 15: der Verifier prüft die Specs mit — saubere Specs gehen durch. Bis Rev. 14
    # wurde hier pauschal blockiert, was Tool-Calling faktisch an `passthrough` band.
    adapter = ToolFakeAdapter()
    outcome = await guarded_completion(
        profile=VERIFYING,
        purpose="external_escalation",
        payload=EgressPayload(raw_text="x", generalized_text="sauber", tools=TOOLS),
        provider_target="claude",
        model="m",
        adapter=adapter,
        audit=AuditLog(),
    )
    assert outcome.released is True
    assert adapter.tools_seen == TOOLS


async def test_verifying_mode_blocks_an_identifier_inside_a_tool_spec() -> None:
    # Die Specs sind geprüfte Fläche: ein Identifier in der Beschreibung blockiert —
    # er wird NICHT wegredigiert (ein umgeschriebenes Schema wäre kaputt, §7.2).
    adapter = ToolFakeAdapter()
    dirty = [
        {
            "name": "ssh",
            "description": "Verbindet auf 192.168.1.10.",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]
    outcome = await guarded_completion(
        profile=VERIFYING,
        purpose="external_escalation",
        payload=EgressPayload(raw_text="x", generalized_text="sauber", tools=dirty),
        provider_target="claude",
        model="m",
        adapter=adapter,
        audit=AuditLog(),
    )
    assert outcome.released is False
    assert "192.168.1.10" in outcome.reason
    assert adapter.calls == []


async def test_tools_reach_the_audit_record() -> None:
    # Was an Tools rausging, muss im reviewbaren Vorher/Nachher stehen (§6).
    audit = AuditLog(level="full")
    await guarded_completion(
        profile=PASSTHROUGH,
        purpose="external_escalation",
        payload=EgressPayload(
            raw_text="x", messages=[{"role": "user", "content": "x"}], tools=TOOLS
        ),
        provider_target="claude",
        model="m",
        adapter=ToolFakeAdapter(),
        audit=audit,
    )
    assert "get_weather" in (audit.entries[-1].after or "")


async def test_non_tool_capable_provider_with_tools_blocks(monkeypatch) -> None:
    # Seit Rev. 15 tragen alle v1-Adapter Tools — das Gate bleibt trotzdem, für den
    # naechsten Adapter, der noch keine Uebersetzung hat. Hier wird genau das geprueft:
    # steht ein Provider NICHT in der Liste, wird abgewiesen statt still ohne Tools gesendet.
    monkeypatch.setattr("sluice.dispatch.TOOL_CAPABLE_PROVIDERS", ("anthropic",))
    adapter = ToolFakeAdapter()
    outcome = await guarded_completion(
        profile=PASSTHROUGH,
        purpose="external_escalation",
        payload=EgressPayload(
            raw_text="x", messages=[{"role": "user", "content": "x"}], tools=TOOLS
        ),
        provider_target="gemini",
        model="m",
        adapter=adapter,
        audit=AuditLog(),
    )
    assert outcome.released is False
    assert adapter.calls == []


# ---------- Adapter: neutrale Spec → nativ, tool_use → neutral (MockTransport) ----------


async def test_anthropic_adapter_renders_tools_and_parses_tool_use() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "claude-sonnet-5",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "text", "text": "Ich rufe das Tool auf."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get_weather",
                        "input": {"city": "Berlin"},
                    },
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = AnthropicAdapter(api_key="k", http_client=client)
        resp = await adapter.complete(
            [{"role": "user", "content": "Wetter in Berlin?"}],
            model="claude-sonnet-5",
            tools=TOOLS,
        )

    # neutrale Spec == Anthropics natives Schema → unverändert gereicht.
    assert captured["body"]["tools"] == TOOLS
    assert resp.text == "Ich rufe das Tool auf."
    assert resp.stop_reason == "tool_use"
    assert resp.tool_calls == (
        ToolCall(id="toolu_1", name="get_weather", arguments={"city": "Berlin"}),
    )


async def test_anthropic_adapter_translates_tool_turn_messages() -> None:
    # Rev. 14: neutrale/OpenAI-Form (Rolle `tool`, assistant.tool_calls) → Anthropic-Blöcke.
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"model": "m", "stop_reason": "end_turn",
                  "content": [{"type": "text", "text": "18 Grad."}]},
        )

    convo = [
        {"role": "system", "content": "du bist knapp"},
        {"role": "user", "content": "Wetter in Berlin?"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "name": "get_weather", "arguments": {"city": "Berlin"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "18C"},
    ]
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = AnthropicAdapter(api_key="k", http_client=client)
        await adapter.complete(convo, model="m")

    body = captured["body"]
    assert body["system"] == "du bist knapp"  # system herausgehoben
    msgs = body["messages"]
    assert [x["role"] for x in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["content"] == "Wetter in Berlin?"  # plain text unverändert
    assert msgs[1]["content"] == [
        {"type": "tool_use", "id": "t1", "name": "get_weather", "input": {"city": "Berlin"}}
    ]
    assert msgs[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "t1", "content": "18C"}
    ]


async def test_anthropic_adapter_merges_consecutive_tool_results() -> None:
    # Anthropic verlangt alle tool_results EINES Turns in EINER user-Nachricht.
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "m", "stop_reason": "end_turn", "content": []})

    convo = [
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "a", "name": "t", "arguments": {}},
                        {"id": "b", "name": "t", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "a", "content": "ra"},
        {"role": "tool", "tool_call_id": "b", "content": "rb"},
    ]
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = AnthropicAdapter(api_key="k", http_client=client)
        await adapter.complete(convo, model="m")

    msgs = captured["body"]["messages"]
    assert [x["role"] for x in msgs] == ["assistant", "user"]  # zwei Results → EINE user-Msg
    assert msgs[1]["content"] == [
        {"type": "tool_result", "tool_use_id": "a", "content": "ra"},
        {"type": "tool_result", "tool_use_id": "b", "content": "rb"},
    ]


# ---------- Endpoint: /v1/chat/completions ----------


def _client(adapter: ToolFakeAdapter) -> httpx.AsyncClient:
    app = create_app(
        {"prismclaw": PASSTHROUGH, "temper": VERIFYING},
        adapter_factory=lambda name: adapter,
        audit=AuditLog(),
    )
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://sluice")


async def test_endpoint_returns_tool_calls() -> None:
    adapter = ToolFakeAdapter(
        tool_calls=(ToolCall(id="t1", name="get_weather", arguments={"city": "Berlin"}),)
    )
    client = _client(adapter)
    resp = await client.post(
        "/v1/chat/completions",
        headers={"X-Sluice-Profile": "prismclaw"},
        json={
            "messages": [{"role": "user", "content": "Wetter?"}],
            "model": "claude-sonnet-5",
            "tools": TOOLS,
        },
    )
    assert resp.status_code == 200
    choice = resp.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"] == [
        {"id": "t1", "name": "get_weather", "arguments": {"city": "Berlin"}}
    ]
    await client.aclose()


async def test_endpoint_stream_with_tools_blocks() -> None:
    # Streaming von Tool-Calls ist in Rev. 14 nicht dabei ⇒ fail-closed statt Drop.
    adapter = ToolFakeAdapter()
    client = _client(adapter)
    resp = await client.post(
        "/v1/chat/completions",
        headers={"X-Sluice-Profile": "prismclaw"},
        json={
            "messages": [{"role": "user", "content": "x"}],
            "model": "m",
            "tools": TOOLS,
            "stream": True,
        },
    )
    assert resp.status_code == 403
    assert adapter.calls == []
    await client.aclose()


async def test_running_tool_turn_without_new_specs_also_checks_the_adapter(monkeypatch) -> None:
    # Ein agentischer Folge-Turn lässt `tools` oft weg, schickt aber `tool_calls` und
    # `role:"tool"` mit. Auch dann muss der Ziel-Adapter die Form lesen können — sonst
    # ginge eine Konversation raus, die der Provider nicht versteht.
    monkeypatch.setattr("sluice.dispatch.TOOL_CAPABLE_PROVIDERS", ("anthropic",))
    audit = AuditLog()
    adapter = ToolFakeAdapter()
    outcome = await guarded_completion(
        profile=PASSTHROUGH,
        purpose="external_escalation",
        payload=EgressPayload(
            raw_text="x",
            messages=[
                {
                    "role": "assistant",
                    "tool_calls": [{"id": "t1", "name": "n", "arguments": {}}],
                },
                {"role": "tool", "tool_call_id": "t1", "content": "r"},
            ],
        ),
        provider_target="gemini",
        model="m",
        adapter=adapter,
        audit=audit,
    )
    assert outcome.released is False
    assert adapter.calls == []
    # Und das Audit meldet KEIN released=true für eine nie gesendete Anfrage.
    assert all(e.released is False for e in audit.entries)


async def test_pseudonymizing_reverses_tool_call_arguments() -> None:
    # Das Modell arbeitet auf Pseudonymen und gibt sie in den Argumenten zurück; der
    # Konsument braucht dort den ECHTEN Wert (§7.2/§8). Ohne dieses Reversal liefe sein
    # Tool auf einem Platzhalter, während der Antworttext schon zurückgemappt ist.
    from sluice.modes.pseudonymizing import PseudonymizingMode
    from sluice.modes import Scope

    mode = PseudonymizingMode()
    scope = Scope(key="s1")
    payload = EgressPayload(
        raw_text="Host 192.168.1.10",
        messages=[{"role": "user", "content": "Host 192.168.1.10"}],
        tools=TOOLS,
    )
    # Was der Modus rausschickt, ist das Pseudonym — das spiegelt der Fake als Tool-Arg.
    sanitized = await mode.forward(payload, scope)
    pseudonym = sanitized.messages[0]["content"].split()[-1]
    assert pseudonym != "192.168.1.10"

    adapter = ToolFakeAdapter(
        tool_calls=(ToolCall(id="t1", name="ssh", arguments={"host": pseudonym}),)
    )
    outcome = await guarded_completion(
        profile=REVERSIBLE,
        purpose="external_escalation",
        payload=payload,
        provider_target="claude",
        model="m",
        scope=scope,
        mode=mode,
        adapter=adapter,
        audit=AuditLog(),
    )
    assert outcome.released is True
    assert outcome.tool_calls == [
        {"id": "t1", "name": "ssh", "arguments": {"host": "192.168.1.10"}}
    ]
