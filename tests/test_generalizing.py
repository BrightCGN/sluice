"""GeneralizingMode — Einbahnstraße (Spec §3): Reverse-Aufrufe werfen."""

from __future__ import annotations

import pytest

from sluice.modes import EgressPayload, Scope
from sluice.modes.generalizing import GeneralizingMode


async def test_forward_passes_consumer_generalized_text() -> None:
    s = GeneralizingMode()
    sanitized = await s.forward(
        EgressPayload(raw_text="konkret 10.0.0.1", generalized_text="generisch"), None
    )
    assert sanitized.text == "generisch"


async def test_forward_without_generalized_text_fails_closed() -> None:
    s = GeneralizingMode()
    with pytest.raises(ValueError):
        await s.forward(EgressPayload(raw_text="konkret"), None)


async def test_reverse_methods_raise_not_implemented() -> None:
    s = GeneralizingMode()
    scope = Scope("s")
    assert s.reversible is False
    with pytest.raises(NotImplementedError):
        await s.reverse_text("x", scope)
    with pytest.raises(NotImplementedError):
        s.stream_reverser(scope)
    with pytest.raises(NotImplementedError):
        await s.reverse_obj({"a": "b"}, scope)
