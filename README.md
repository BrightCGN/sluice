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
- **`passthrough`** — kein Verifier, keine Transformation; **explizites Opt-in im Profil** (`allowed_modes`, §4.1) — eine leere Allowlist sperrt ihn fail-closed (Rev. 11), die Request-Wahl allein reicht nicht. Der Konsument trägt das Risiko (§2.1).
- **`generalizing`** — verifiziert nur den vom Konsumenten *bereits* generalisierten Text.
- **`pseudonymizing`** (Opt-in) — forward + reverse mit Streaming-Holdback, Tool-Arg-Reversal und session-scoped Mapping (TTL, deterministisches Aufräumen).
- **`pii_regex`** (Rev. 12) — deutsche PII über `pii_de`-Muster **mit Prüfziffernverfahren** (IBAN Mod-97, Luhn, Steuer-ID, SVNR, KVNR), span-basiert redigiert. Kein Dienst, kein Modell, Latenz im Mikrosekundenbereich.
- **`pii_ner`** (Rev. 12) — `pii_regex` **plus** Modellerkennung, **additiv**: beide Stufen laufen, das Ergebnis ist die Vereinigungsmenge; bei Überlappung gewinnt Regex, weil nur die Regex-Stufe den *validierten* Typ kennt. Als Unterklasse von `pii_regex` gebaut, damit die Regex-Stufe strukturell nicht wegfallen kann. Braucht den NER-Dienst (§7.5) — **ohne ihn wird blockiert, nie degradiert**. Lange Texte werden in überlappende Stücke zerlegt: das Modell kürzt darüber hinaus *still*, und eine Kürzung ließe den hinteren Teil ungeprüft durch, während das Audit „geprüft" meldete.

Fehlt `mode`, gilt der sichere Default `strict` — nie `passthrough` (safe by default).

> **Namensregel-Ausnahme (Rev. 12):** Modus-Namen benennen sonst die *Absicht*, nicht die
> Engine. Für `pii_regex`/`pii_ner` ist die Regel bewusst aufgehoben, weil die
> **Zweistufigkeit selbst** die Zusage ist. Für neue Modi gilt sie weiter.

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

Kern + Modi + HTTP-Service (`/v1/…`) stehen. **Rev. 9** macht Sluice zum
erweiterbaren Modus-Framework (`strict`-Default, `passthrough`, `allowed_modes`,
konfigurierbares Audit). **Rev. 12** ergänzt die zweistufige PII-Erkennung
(`pii_regex`/`pii_ner`), den eigenständigen NER-Dienst (§7.5) und die
Anonymisierungs-Identität (§5.4).

Auf der Sluice-VM ausgerollt sind Kern und Provider-Gateways. Der NER-Dienst ist
**noch nicht in Betrieb**: die Latenzmessung (2026-08-11) liegt vor, die Modellwahl
und die Schwellwert-Kalibrierung stehen aus — beides braucht einen deutschsprachigen
Evaluationsdatensatz. Details und offene Punkte: `docs/NER-SERVICE.md` §9.

Offen (§10): Open-Source-Split (generischer Kern öffentlich, Homelab-/Konsumenten-Details
privat), öffentliches Modus-Plugin-API, Lizenzwahl.
