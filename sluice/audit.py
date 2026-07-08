"""Audit — `egress_log`, append-only (Spec §6).

Genau *ein* Log, das der Guard bei **jedem** Durchlass schreibt — released *und*
blocked. Das reviewbare Vorher/Nachher ist die Fläche fürs Admin-Gate und der
DSGVO-/Audit-Nachweis (Prinzip 13). Löst Tempers `TODO(post-v1)` in
`guard.py::_log_egress` ein.

v1: strukturiertes Logging + in-memory Append-Store; Persistenz ist post-v1 (Spec §10).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog

log = structlog.get_logger("sluice.audit")


@dataclass(frozen=True)
class EgressLogEntry:
    """Ein Eintrag im egress_log — Felder gemäß Spec §6."""

    timestamp: datetime
    profile: str | None
    purpose: str
    strategy: str | None
    released: bool
    reason: str
    before: str
    after: str | None
    provider_target: str | None
    verifier_findings: tuple[str, ...] = field(default_factory=tuple)


class AuditLog:
    """Append-only Store für egress_log-Einträge (Spec §6, Prinzip 13).

    Bewusst keine Lösch-/Änderungs-API: Einträge kommen nur dazu. Getrennte
    Audit-Streams pro Profil sind Sicht auf dasselbe Log (Filter über `profile`,
    Mandanten-Isolation §4.4).
    """

    def __init__(self) -> None:
        self._entries: list[EgressLogEntry] = []

    def append(
        self,
        *,
        profile: str | None,
        purpose: str,
        strategy: str | None,
        released: bool,
        reason: str,
        before: str,
        after: str | None,
        provider_target: str | None = None,
        verifier_findings: tuple[str, ...] = (),
    ) -> EgressLogEntry:
        entry = EgressLogEntry(
            timestamp=datetime.now(UTC),
            profile=profile,
            purpose=purpose,
            strategy=strategy,
            released=released,
            reason=reason,
            before=before,
            after=after,
            provider_target=provider_target,
            verifier_findings=verifier_findings,
        )
        self._entries.append(entry)
        log.info(
            "egress",
            profile=profile,
            purpose=purpose,
            strategy=strategy,
            released=released,
            reason=reason,
            provider_target=provider_target,
            findings=list(verifier_findings),
        )
        return entry

    @property
    def entries(self) -> tuple[EgressLogEntry, ...]:
        """Read-only Sicht auf alle Einträge (reviewbares Vorher/Nachher)."""
        return tuple(self._entries)

    def for_profile(self, profile: str) -> tuple[EgressLogEntry, ...]:
        """Audit-Stream eines Profils (Mandanten-Isolation, §4.4)."""
        return tuple(e for e in self._entries if e.profile == profile)


# Prozessweiter Default-Store (v1). Konsumenten des Guards können einen eigenen
# AuditLog injizieren (Tests); der Dienst nutzt diesen einen.
egress_log = AuditLog()
