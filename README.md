# Sluice

Die **eine gemeinsame Sanitisierungs-Boundary**, durch die jeder ausgehende Datenpfad
aller Projekte läuft, bevor er die Kundengrenze überquert. Sluice sitzt **vor** dem
PrismClaw-Routing-Gateway: Sluice sanitisiert, das Gateway routet.

**Wahrheitsquelle für alle Design-Entscheidungen: [`docs/SLUICE-BOUNDARY-SPEC.md`](docs/SLUICE-BOUNDARY-SPEC.md).**
Leitplanken für die Arbeit am Repo: [`CLAUDE.md`](CLAUDE.md).

## Die Kette

Jeder Egress läuft durch dieselbe geguardete Kette:

```
Profil-Gate (§4) → Strategie (§3) → Verifier (§5) → Audit (§6)
```

Drei Invarianten, die der Strategie-Schalter nie verändert:

1. **Profil-Gate zuerst** — kein Profil → nichts raus (Default-Deny); `egress_enabled=false` → nichts raus.
2. **Verifier gleich streng in beiden Modi** — der deterministische Riegel liegt *unter* der Strategie. Fail-closed.
3. **Audit bei jedem Durchlass** — append-only, released *und* blocked, reviewbares Vorher/Nachher.

## Strategien

- **`GeneralizingStrategy`** (Default) — Einbahnstraße, irreversibel, zustandslos. Die
  *semantische* Generalisierung macht der Konsument; Sluice verifiziert nur.
- **`PseudonymizingStrategy`** (explizites Opt-in) — forward + reverse mit
  Streaming-Holdback, Tool-Arg-Reversal und session-scoped Mapping (TTL, deterministisches
  Aufräumen).

## Verwendung (Phase 1+2: Library)

```python
from sluice import EgressPayload, Profile, guarded_egress

profile = Profile(
    name="temper",
    strategy="generalizing",
    allowed_purposes=("external_escalation",),
    provider_allowlist=("claude",),
    detector_profile="infra",
)

outcome = await guarded_egress(
    profile=profile,
    purpose="external_escalation",
    payload=EgressPayload(raw_text="…konkret…", generalized_text="…generalisiert…"),
)
if outcome.released:
    dispatch(outcome.sanitized_text)  # der Konsument schickt selbst ans Gateway
```

## Entwicklung

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest
```

## Stand

Phase 1 (Kern) + Phase 2 (Strategien) — testbare Library, noch keine HTTP-Oberfläche.
Phase 3 (Endpoints `/v1/…` + Client-SDK), Phase 4 (Konsumenten-Migration) und
Phase 5 (Aufräumen) folgen.
