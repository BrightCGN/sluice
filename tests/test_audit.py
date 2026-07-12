"""Audit-Level-Tests (Spec §6, Rev. 9) — Betreiber wählt off | metadata | full."""

from __future__ import annotations

from sluice.audit import AuditLog

_KW = dict(
    profile="p",
    purpose="x",
    mode="strict",
    released=True,
    reason="clean",
    before="192.168.1.1 roh",
    after="[IP] roh",
    provider_target="claude",
)


def test_default_level_is_metadata() -> None:
    assert AuditLog().level == "metadata"


def test_metadata_level_keeps_entry_but_drops_payload() -> None:
    audit = AuditLog(level="metadata")
    audit.append(**_KW)
    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry.mode == "strict"
    assert entry.released is True
    assert entry.before is None  # Nutzdaten werden nicht persistiert
    assert entry.after is None


def test_full_level_keeps_payload() -> None:
    audit = AuditLog(level="full")
    audit.append(**_KW)
    entry = audit.entries[0]
    assert entry.before == "192.168.1.1 roh"
    assert entry.after == "[IP] roh"


def test_off_level_writes_nothing() -> None:
    audit = AuditLog(level="off")
    result = audit.append(**_KW)
    assert result is None
    assert audit.entries == ()


def test_unknown_level_falls_back_to_metadata() -> None:
    # Im Zweifel protokollieren, nicht schweigen (fail-safe, nicht off).
    assert AuditLog(level="bogus").level == "metadata"
