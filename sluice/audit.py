"""Audit — `egress_log`, append-only (Spec §6, Rev. 9).

*Ein* Log, das der Guard beim Durchlass schreibt — released *und* blocked. Löst Tempers
`TODO(post-v1)` in `guard.py::_log_egress` ein.

**Detailgrad konfiguriert der Betreiber (Rev. 9), nicht der Modus** — global per
`SLUICE_AUDIT_LEVEL`:
- `full`     — voller Eintrag inkl. reviewbarem Vorher/Nachher (`before`/`after`).
- `metadata` — Default: Eintrag *ohne* `before`/`after` (man sieht *dass* etwas durchging
               und in welchem Modus, ohne die Nutzdaten zu persistieren).
- `off`      — kein Eintrag.

Der Detailgrad ist eine Betriebs-Einstellung und verändert den Sanitisierungs-Modus nicht.
Wo geschrieben wird, ist der Store append-only (Prinzip 13). v1: strukturiertes Logging +
in-memory Append-Store; Persistenz ist post-v1 (Spec §10).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog

log = structlog.get_logger("sluice.audit")

VALID_LEVELS = ("off", "metadata", "full")
DEFAULT_LEVEL = "metadata"  # Rev. 9, §6


def _resolve_level(level: str | None) -> str:
    """Audit-Level aus Argument > Env `SLUICE_AUDIT_LEVEL` > Default `metadata`; unbekannt
    fällt fail-safe auf `metadata` zurück (nicht `off` — im Zweifel lieber protokollieren)."""
    raw = (level if level is not None else os.environ.get("SLUICE_AUDIT_LEVEL", "")).strip().lower()
    if raw in VALID_LEVELS:
        return raw
    if raw:
        log.warning("audit.unknown_level", level=raw, fallback=DEFAULT_LEVEL)
    return DEFAULT_LEVEL


@dataclass(frozen=True)
class EgressLogEntry:
    """Ein Eintrag im egress_log — Felder gemäß Spec §6.

    `before`/`after` tragen nur bei Level `full` Inhalt; bei `metadata` sind sie None.
    """

    timestamp: datetime
    profile: str | None
    purpose: str
    mode: str | None
    released: bool
    reason: str
    before: str | None
    after: str | None
    provider_target: str | None
    verifier_findings: tuple[str, ...] = field(default_factory=tuple)
    # Rev. 16 (§4.5/§6): WARUM dieses Ziel? Nur gesetzt, wenn **Sluice** es gewählt hat
    # (Rotation, `model: "auto"`). Sobald die Wahl nicht mehr allein aus dem Profil
    # ableitbar ist, muss die Begründung im Eintrag stehen — sonst sagt das Log, wohin
    # etwas ging, aber nicht mehr, warum dorthin, und der Eintrag ist keine
    # vollständige Auskunft mehr. None = der Konsument hat das Ziel selbst benannt.
    provider_selection: str | None = None


class AuditLog:
    """Append-only Store für egress_log-Einträge (Spec §6, Prinzip 13).

    Bewusst keine Lösch-/Änderungs-API: Einträge kommen nur dazu. Getrennte
    Audit-Streams pro Profil sind Sicht auf dasselbe Log (Filter über `profile`,
    Mandanten-Isolation §4.4). `level` (Betreiber-Config §6) steuert den Detailgrad.
    """

    def __init__(self, level: str | None = None) -> None:
        self.level = _resolve_level(level)
        self._entries: list[EgressLogEntry] = []

    def append(
        self,
        *,
        profile: str | None,
        purpose: str,
        mode: str | None,
        released: bool,
        reason: str,
        before: str,
        after: str | None,
        provider_target: str | None = None,
        verifier_findings: tuple[str, ...] = (),
        provider_selection: str | None = None,
    ) -> EgressLogEntry | None:
        """Schreibt einen Eintrag gemäß Betreiber-Level. `off` → kein Eintrag (None);
        `metadata` → ohne `before`/`after`; `full` → mit reviewbarem Vorher/Nachher."""
        if self.level == "off":
            return None

        keep_payload = self.level == "full"
        entry = EgressLogEntry(
            timestamp=datetime.now(UTC),
            profile=profile,
            purpose=purpose,
            mode=mode,
            released=released,
            reason=reason,
            before=before if keep_payload else None,
            after=after if keep_payload else None,
            provider_target=provider_target,
            verifier_findings=verifier_findings,
            provider_selection=provider_selection,
        )
        self._entries.append(entry)
        log.info(
            "egress",
            profile=profile,
            purpose=purpose,
            mode=mode,
            released=released,
            reason=reason,
            provider_target=provider_target,
            provider_selection=provider_selection,
            findings=list(verifier_findings),
        )
        return entry

    @property
    def entries(self) -> tuple[EgressLogEntry, ...]:
        """Read-only Sicht auf alle Einträge (bei `full`: reviewbares Vorher/Nachher)."""
        return tuple(self._entries)

    def for_profile(self, profile: str) -> tuple[EgressLogEntry, ...]:
        """Audit-Stream eines Profils (Mandanten-Isolation, §4.4)."""
        return tuple(e for e in self._entries if e.profile == profile)


# Prozessweiter Default-Store (v1). Konsumenten des Guards können einen eigenen
# AuditLog injizieren (Tests); der Dienst nutzt diesen einen. Level aus der Env.
egress_log = AuditLog()
