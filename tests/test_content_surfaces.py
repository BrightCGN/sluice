"""Content-Flächen (§5.5) — die Fläche, die Modus und Verifier sehen.

Regressionsnetz für ein Loch, das bis hierher offen war: `content` ist in der Praxis
nicht immer ein String. Block-Listen, Tool-Result-Inhalte und Tool-Argumente wurden
weder redigiert noch verifiziert und gingen mit `released=true` ungeprüft raus.

Die Tests prüfen beide Hälften: die Aufzählung selbst (`sluice/content.py`) und dass
Guard/Modi tatsächlich darauf aufsetzen.
"""

from __future__ import annotations

import httpx
import pytest

from sluice.audit import AuditLog
from sluice.content import (
    ContentShapeError,
    message_surfaces,
    messages_surfaces,
    rebuild_message,
)
from sluice.guard import guarded_egress
from sluice.modes import EgressPayload, Scope
from sluice.modes.pii_regex import PiiRegexMode
from sluice.modes.pseudonymizing import PseudonymizingMode
from sluice.modes.strict import StrictMode
from sluice.policy import Profile, ReversibleConfig
from sluice.providers import ProviderError
from sluice.providers.gemini import GeminiAdapter

STRICT = Profile(
    name="strict-profil",
    mode="strict",
    egress_enabled=True,
    allowed_purposes=("external_escalation",),
    provider_allowlist=("claude",),
    detector_profile="infra",
)

VERIFY_ONLY = Profile(
    name="temper",
    mode="generalizing",
    egress_enabled=True,
    allowed_purposes=("external_escalation",),
    provider_allowlist=("claude",),
    detector_profile="infra",
)

PASSTHROUGH = Profile(
    name="prismclaw",
    mode="passthrough",
    egress_enabled=True,
    allowed_purposes=("external_escalation",),
    provider_allowlist=("claude",),
    detector_profile="infra",
    allowed_modes=("passthrough",),
)

REVERSIBLE = Profile(
    name="aider",
    mode="pseudonymizing",
    egress_enabled=True,
    allowed_purposes=("code_completion",),
    provider_allowlist=("claude",),
    detector_profile="infra",
    reversible=ReversibleConfig(),
)


# ---------- Aufzählung: welche Flächen erkennt der Walker? ----------


def test_plain_string_content() -> None:
    s = message_surfaces({"role": "user", "content": "hallo"})
    assert s.texts == ("hallo",)
    assert s.opaque == ()


def test_text_block_list() -> None:
    # Die Normalform bei Anthropic und bei jedem OpenAI-Client mit Bildern/Tools.
    s = message_surfaces(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "erste Fläche"},
                {"type": "text", "text": "zweite Fläche"},
            ],
        }
    )
    assert s.texts == ("erste Fläche", "zweite Fläche")
    assert s.opaque == ()


def test_openai_responses_block_types() -> None:
    s = message_surfaces(
        {"role": "user", "content": [{"type": "input_text", "text": "rein"}]}
    )
    assert s.texts == ("rein",)


def test_tool_result_string_and_blocks() -> None:
    s = message_surfaces(
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "18 Grad"},
                {
                    "type": "tool_result",
                    "tool_use_id": "t2",
                    "content": [{"type": "text", "text": "verschachtelt"}],
                },
            ],
        }
    )
    assert s.texts == ("18 Grad", "verschachtelt")
    assert s.opaque == ()


def test_tool_use_arguments_values_are_text_keys_are_readonly() -> None:
    s = message_surfaces(
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "ssh",
                    "input": {"host": "192.168.1.10", "port": 22},
                }
            ],
        }
    )
    assert s.texts == ("192.168.1.10",)  # nur String-Werte sind Inhalt (22 ist eine Zahl)
    assert s.readonly == ("host", "port")  # Schlüssel werden geprüft, nie überschrieben
    assert s.opaque == ()


def test_neutral_and_openai_tool_calls_on_a_message() -> None:
    neutral = message_surfaces(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "t1", "name": "ssh", "arguments": {"host": "10.0.0.1"}}],
        }
    )
    assert neutral.texts == ("", "10.0.0.1")

    openai = message_surfaces(
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "t1",
                    "type": "function",
                    "function": {"name": "ssh", "arguments": '{"host": "10.0.0.1"}'},
                }
            ],
        }
    )
    # OpenAI-Dialekt: `arguments` ist ein JSON-String und bleibt als Fläche einer.
    assert openai.texts == ('{"host": "10.0.0.1"}',)


def test_protocol_identifiers_are_not_surfaces() -> None:
    # role / tool_call_id / id / name sind Vertragswerte, kein Nutzerinhalt.
    s = message_surfaces({"role": "tool", "tool_call_id": "t1", "content": "x"})
    assert s.texts == ("x",)
    assert "t1" not in s.verifiable and "tool" not in s.verifiable


def test_unknown_block_is_opaque_not_silently_passed() -> None:
    s = message_surfaces(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Beschreibung"},
                {"type": "image", "source": {"data": "…base64…"}},
            ],
        }
    )
    assert s.texts == ("Beschreibung",)
    assert len(s.opaque) == 1
    assert "image" in s.opaque[0]
    # Der Inhalt selbst steht NIE in der Beschreibung (die läuft ins Audit/den Reason).
    assert "base64" not in s.opaque[0]


def test_non_dict_content_is_opaque() -> None:
    assert message_surfaces({"role": "user", "content": 42}).opaque


# ---------- Aufzählen und Ersetzen laufen über denselben Walker ----------


@pytest.mark.parametrize(
    "message",
    [
        {"role": "user", "content": "schlicht"},
        {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "r"}]},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t", "name": "n", "input": {"k": "v"}}],
        },
        {"role": "assistant", "tool_calls": [{"id": "t", "name": "n", "arguments": {"k": "v"}}]},
    ],
)
def test_identity_rebuild_is_lossless(message: dict) -> None:
    surfaces = message_surfaces(message)
    assert rebuild_message(message, surfaces.texts) == message


def test_rebuild_actually_replaces_every_surface() -> None:
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "a"},
            {"type": "tool_result", "tool_use_id": "t", "content": "b"},
        ],
    }
    out = rebuild_message(message, ["A", "B"])
    assert out["content"][0]["text"] == "A"
    assert out["content"][1]["content"] == "B"
    assert message["content"][0]["text"] == "a"  # Original unangetastet


def test_rebuild_mismatch_fails_loudly() -> None:
    # Ein Bruch zwischen Aufzählung und Ersetzung wird nie stillschweigend zurechtgebogen.
    with pytest.raises(ContentShapeError):
        rebuild_message({"role": "user", "content": "a"}, ["x", "y"])


def test_messages_surfaces_keeps_order() -> None:
    s = messages_surfaces(
        [{"role": "user", "content": "eins"}, {"role": "assistant", "content": "zwei"}]
    )
    assert s.texts == ("eins", "zwei")


# ---------- Regression: der Riegel greift jetzt auch auf diesen Flächen ----------


async def test_strict_redacts_inside_block_content() -> None:
    # VOR dem Fix: unredigiert und ungeprüft durchgereicht.
    sanitized = await StrictMode(detector_profile="infra").forward(
        EgressPayload(
            raw_text="",
            messages=[
                {"role": "user", "content": [{"type": "text", "text": "Host 192.168.1.10"}]}
            ],
        ),
        None,
    )
    assert sanitized.messages[0]["content"][0]["text"] == "Host [IP]"


async def test_strict_redacts_inside_tool_result_and_tool_arguments() -> None:
    sanitized = await StrictMode(detector_profile="infra").forward(
        EgressPayload(
            raw_text="",
            messages=[
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "t1", "name": "ssh", "arguments": {"host": "192.168.1.10"}}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "ok, a@b.de"}
                    ],
                },
            ],
        ),
        None,
    )
    assert sanitized.messages[0]["tool_calls"][0]["arguments"] == {"host": "[IP]"}
    assert sanitized.messages[1]["content"][0]["content"] == "ok, [EMAIL]"


async def test_pii_regex_redacts_inside_block_content() -> None:
    sanitized = await PiiRegexMode(detector_profile="pii_de").forward(
        EgressPayload(
            raw_text="",
            messages=[{"role": "user", "content": [{"type": "text", "text": "mail a@b.de"}]}],
        ),
        None,
    )
    assert "a@b.de" not in sanitized.messages[0]["content"][0]["text"]


async def test_verifier_sees_identifier_hidden_in_a_block() -> None:
    # `generalizing` transformiert nicht — der Verifier ist hier allein der Riegel.
    # VOR dem Fix: released=True, weil die Fläche nie geprüft wurde.
    outcome = await guarded_egress(
        profile=VERIFY_ONLY,
        purpose="external_escalation",
        payload=EgressPayload(
            raw_text="",
            messages=[{"role": "user", "content": [{"type": "text", "text": "IP 192.168.1.10"}]}],
        ),
        audit=AuditLog(),
    )
    assert outcome.released is False
    assert "192.168.1.10" in outcome.reason


async def test_verifier_sees_identifier_in_a_tool_argument() -> None:
    outcome = await guarded_egress(
        profile=VERIFY_ONLY,
        purpose="external_escalation",
        payload=EgressPayload(
            raw_text="",
            messages=[
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "t1", "name": "ssh", "arguments": {"h": "192.168.1.10"}}
                    ],
                }
            ],
        ),
        audit=AuditLog(),
    )
    assert outcome.released is False


async def test_identifier_in_an_argument_key_blocks_instead_of_being_rewritten() -> None:
    # readonly-Fläche: wird geprüft, nie überschrieben — sonst könnten zwei Schlüssel
    # auf denselben Platzhalter fallen und ein Feld still verschwinden.
    outcome = await guarded_egress(
        profile=STRICT,
        purpose="external_escalation",
        payload=EgressPayload(
            raw_text="",
            messages=[
                {
                    "role": "assistant",
                    "tool_calls": [{"id": "t", "name": "n", "arguments": {"192.168.1.10": "x"}}],
                }
            ],
        ),
        audit=AuditLog(),
    )
    assert outcome.released is False


# ---------- Nicht prüfbare Flächen: blockieren, nicht überspringen ----------


async def test_opaque_block_is_blocked_under_a_verifying_mode() -> None:
    outcome = await guarded_egress(
        profile=STRICT,
        purpose="external_escalation",
        payload=EgressPayload(
            raw_text="",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "harmlos"},
                        {"type": "image", "source": {"data": "…"}},
                    ],
                }
            ],
        ),
        audit=AuditLog(),
    )
    assert outcome.released is False
    assert "Nicht prüfbare Content-Fläche" in outcome.reason


async def test_opaque_block_passes_under_passthrough() -> None:
    # `passthrough` komponiert den Verifier bewusst nicht (§2.1) — die Regel gilt
    # generisch über enforce_verifier, nicht am Namen.
    outcome = await guarded_egress(
        profile=PASSTHROUGH,
        purpose="external_escalation",
        payload=EgressPayload(
            raw_text="",
            messages=[{"role": "user", "content": [{"type": "image", "source": {"data": "…"}}]}],
        ),
        audit=AuditLog(),
    )
    assert outcome.released is True


# ---------- Pseudonymizing: Hin- und Rückweg über dieselben Flächen ----------


async def test_pseudonymizing_covers_tool_arguments_and_reverses_them() -> None:
    mode = PseudonymizingMode()
    scope = Scope(key="s1")
    sanitized = await mode.forward(
        EgressPayload(
            raw_text="",
            messages=[
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "t1", "name": "ssh", "arguments": {"host": "192.168.1.10"}}
                    ],
                }
            ],
        ),
        scope,
    )
    pseudonym = sanitized.messages[0]["tool_calls"][0]["arguments"]["host"]
    assert pseudonym != "192.168.1.10"  # VOR dem Fix stand hier der rohe Wert

    # Tool-Arg-Reversal (§7.2): der Konsument führt das Tool auf dem ECHTEN Wert aus.
    back = await mode.reverse_obj({"host": pseudonym}, scope)
    assert back["host"] == "192.168.1.10"


# ---------- Adapter: kein stilles Verwerfen ----------


async def test_gemini_adapter_fails_closed_on_block_content() -> None:
    # VOR dem Fix: die Message wurde übersprungen — das Modell sah weniger, als die
    # Boundary freigegeben und das Audit protokolliert hat.
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("darf nie gesendet werden")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GeminiAdapter(api_key="k", http_client=client)
        with pytest.raises(ProviderError):
            await adapter.complete(
                [{"role": "user", "content": [{"type": "text", "text": "x"}]}], model="m"
            )
