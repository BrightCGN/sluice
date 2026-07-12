# Sluice

Ein **erweiterbares Egress-Sanitisierungs-Framework** (Rev. 9): eine gemeinsame
Sanitisierungs-Boundary, durch die ein ausgehender Datenpfad läuft, bevor er die
Kundengrenze überquert. Sluice ist ein **technischer** Egress-Riegel — **kein
Compliance-Zertifikat**; welche rechtliche Aussage ein Betreiber daraus ableitet, ist
dessen Sache, nicht Sluices Versprechen.

**Wahrheitsquelle für alle Design-Entscheidungen: [`docs/SLUICE-BOUNDARY-SPEC.md`](docs/SLUICE-BOUNDARY-SPEC.md).**
Leitplanken für die Arbeit am Repo: [`CLAUDE.md`](CLAUDE.md).

## Die Kette

Jeder Egress läuft durch dieselbe geguardete Kette:

```
Profil-Gate (§4) → Modus (§3, Registry) → (Verifier §5) → Audit (§6)
```

Der **`strict`-Default** (auto-redigierend) garantiert, was bis Rev. 8 die „drei
Invarianten" waren:

1. **Profil-Gate zuerst** — kein Profil → nichts raus (Default-Deny); `egress_enabled=false` → nichts raus, egal welcher Modus.
2. **Verifier fail-closed** — der deterministische Riegel als Baustein *unter* dem Modus; `strict`/`generalizing`/`pseudonymizing` komponieren ihn, `passthrough` bewusst nicht.
3. **Audit** — Detailgrad wählt der Betreiber (`off | metadata | full`).

## Modi (Registry, §3)

Ein Modus ist ein austauschbarer Egress-Handler; Dritte registrieren eigene über
`register_mode`. Eingebaut:

- **`strict`** (Default) — auto-redigierend über die Detektor-Muster, irreversibel, Verifier fail-closed.
- **`passthrough`** — kein Verifier, keine Transformation; **explizites Opt-in**, der Konsument trägt das Risiko (§2.1).
- **`generalizing`** — verifiziert nur den vom Konsumenten *bereits* generalisierten Text.
- **`pseudonymizing`** (Opt-in) — forward + reverse mit Streaming-Holdback, Tool-Arg-Reversal und session-scoped Mapping (TTL, deterministisches Aufräumen).

Fehlt `mode`, gilt der sichere Default `strict` — nie `passthrough` (safe by default).

## Verwendung (Library)

```python
from sluice import EgressPayload, Profile, guarded_egress

profile = Profile(
    name="temper",
    mode="strict",                       # Default; fehlt das Feld, gilt ebenfalls strict
    allowed_purposes=("external_escalation",),
    provider_allowlist=("claude",),
    detector_profile="infra",
)

outcome = await guarded_egress(
    profile=profile,
    purpose="external_escalation",
    payload=EgressPayload(raw_text="Host 10.0.0.5 meldet Druck"),
)
if outcome.released:
    dispatch(outcome.sanitized_text)     # z. B. "Host [IP] meldet Druck"
```

## Entwicklung

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest
```

## Stand

Kern + Strategien + HTTP-Service (`/v1/…`) stehen. **Rev. 9** macht Sluice zum
erweiterbaren Modus-Framework (`strict`-Default, `passthrough`, `allowed_modes`,
konfigurierbares Audit). Offen (§10): Open-Source-Split (generischer Kern öffentlich,
Homelab-/Konsumenten-Details privat), öffentliches Modus-Plugin-API, Lizenzwahl.
