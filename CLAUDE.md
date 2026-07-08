# CLAUDE.md — Sluice

Leitplanken für Arbeit an diesem Repo. **Wahrheitsquelle für alle Design-Entscheidungen ist
`docs/SLUICE-BOUNDARY-SPEC.md`** — diese Datei fasst nur die Regeln zusammen, die *nie* aus dem
eigenen Urteil heraus gebrochen werden dürfen. Bei Widerspruch gewinnt die Spec; im Zweifel
nachfragen statt raten.

---

## Was Sluice ist

Die **eine gemeinsame Sanitisierungs-Boundary**, durch die jeder ausgehende Datenpfad aller
Projekte läuft, bevor er die Kundengrenze überquert. Eigenständiger Dienst, eigener
Lebenszyklus. Seit **Spec-Revision 2 (2026-07-08)** spricht Sluice die Provider (Claude,
OpenAI, Gemini, Mistral) **selbst** über Adapter an (§7.3) — das PrismClaw-Gateway liegt nicht
mehr im Pfad. Konsumenten binden ein dünnes Client-SDK ein.

Sluice ist das DSGVO-Argument in Code-Form: **eine** Policy, **ein** Audit-Log, **ein**
deterministischer Riegel, auf den bei einer Prüfung gezeigt wird.

---

## Die drei Invarianten (NIE verändern)

Jeder Egress läuft durch dieselbe Kette. Der Strategie-**Schalter** tauscht nur das
Strategie-Objekt — er verändert diese drei Schichten nicht:

1. **Profil-Gate zuerst.** Kein Profil → nichts raus (Default-Deny). `egress_enabled=false`
   (souverän) → nichts raus, egal welche Strategie.
2. **Verifier gleich streng in beiden Modi.** Der deterministische Riegel (`verify_no_identifiers`)
   liegt *unter* der Strategie, nicht in ihr. „Reversibel" ist **kein** Grund, ihn zu lockern —
   auch dann darf nur ein *Pseudonym* raus, nie ein echter Wert, den das Mapping übersah.
   Fail-closed: findet er irgendeinen rohen Identifier → blockieren.
3. **Audit bei jedem Durchlass.** Append-only, released *und* blocked, reviewbares Vorher/Nachher.

---

## Mechanismus vs. Domäne — die Grenze, die du NICHT selbst ziehst

Das ist die zentrale Urteilsentscheidung des Projekts (Spec §1.1). Sie falsch zu ziehen macht
Sluice undicht oder nicht wiederverwendbar. **Nicht selbst raten — gegen die Spec bauen.**

- **In Sluice (generischer Mechanismus):** Profil-Gate, Verifier-Engine, Audit, Diff-Mechanik,
  das Ausführen der gewählten Strategie.
- **Beim Konsumenten (Domäne/Policy):** die **semantische Generalisierung** (aus einem
  validierten Fix die übertragbare Lektion machen — das ist Temper-Businesslogik, nicht Sluice),
  die konkreten Detektor-**Muster** (als Profil deklariert), die Provider-Allowlist.
- **Seit Revision 2 in Sluice:** Provider-Adapter (anthropic/openai/gemini/mistral, §7.3)
  inkl. API-Keys per Env — Adapter werden **nur nach `released=true`** aufgerufen, nie davor.
- **Nicht in Sluice (post-v1 offen):** Failover/Health/Tenant-Order-Routing.

Wenn Domänenlogik „mal eben" nach Sluice greifen will: **das ist das Signal zu stoppen**, nicht
weiterzumachen.

---

## Strategie-Schalter

`SanitizationStrategy` (Protocol) mit zwei Implementierungen, profilgewählt:

- **`GeneralizingStrategy`** — `reversible=False`, **DEFAULT (Spec-Revision 4, safety
  first)**. Einbahnstraße: `forward()` nur; Reverse-Methoden werfen `NotImplementedError`.
  Herkunft: Tempers `egress/`-Datenfluss.
- **`PseudonymizingStrategy`** — `reversible=True`, **explizites Opt-in** (per Profil oder
  Request-`mode: "reversible"`). forward + reverse + `stream_reverser` (Holdback-Puffer) +
  `reverse_obj` (Tool-Args) + Mapping-Lebenszyklus. Herkunft: PrismClaws `prismclaw.anon`.

Reversibel ist die schwächere DSGVO-Zusage (Mapping-Tabelle bleibt personenbezogen) und führt
Zustand ein — daher **nie geerbt, immer bewusst deklariert**.

---

## Repo-Struktur (Zielbild)

```
sluice/
  guard.py               # Orchestrierung: Gate → Strategie → Verifier → Audit
  policy.py              # Profil-Schema + check_egress_allowed
  verifier.py            # deterministischer Riegel; Muster aus Detektor-Profilen
  audit.py               # egress_log, append-only
  strategies/
    __init__.py          # SanitizationStrategy (Protocol) + Registry
    generalizing.py      # forward-only
    pseudonymizing.py    # forward + reverse + stream_reverser + reverse_obj + scope/TTL
  detectors/             # Muster-Sets: infra, code, media, financial
  dispatch.py            # guarded_completion: Guard → Provider-Adapter → (reverse)
  server.py              # eigenständiger HTTP-Service: /v1/egress/guard + /v1/chat/completions
  providers/
    __init__.py          # ProviderAdapter (Protocol) + Registry, Keys per Env
    anthropic.py         # Claude (Messages-API)
    openai_compat.py     # gemeinsamer Chat-Completions-Dialekt
    openai.py            # OpenAI
    mistral.py           # Mistral AI
    gemini.py            # Google Gemini (generateContent)
docs/
  SLUICE-BOUNDARY-SPEC.md  # Wahrheitsquelle
tests/                   # kein Netz; httpx.MockTransport wo HTTP nötig
```

---

## Code-Konventionen (aus dem bestehenden Code)

- Python 3.11+, `from __future__ import annotations`, durchgehend Type-Hints, `str | None`-Unions.
- `@dataclass` / `@dataclass(frozen=True)` für Ergebnis-/Config-Objekte. `Protocol` für Interfaces.
- `async` durchgängig; `httpx.AsyncClient` für HTTP. **Kein Read-Timeout** bei Streaming
  (agentische Turns streamen lange), endlicher Connect-Timeout.
- Strukturiertes Logging (`structlog`, wie im Gateway).
- **Deutsche Docstrings mit §-Ankern**, die auf `SLUICE-BOUNDARY-SPEC.md` verweisen — wie in
  `PROVIDER-GATEWAY.md` / `verifier.py`.
- Fail-closed als Grundhaltung: im Zweifel blockieren, nicht durchlassen.

---

## Test-Disziplin

- **Verhalten erhalten.** Das ist eine Extraktion aus laufendem Code, kein Neubau. Die zwei
  Referenz-Testsuiten sind das Sicherheitsnetz und wandern mit: Tempers `test_egress_guard.py`
  und PrismClaws `anon`-Tests.
- Kein Netz in Tests; wo HTTP nötig, `httpx.MockTransport`.
- Pflicht-Fälle: Verifier gleich streng in beiden Modi · Default-Deny (kein Profil → nichts raus)
  · souveränes Profil (`egress_enabled=false` → nichts raus) · Streaming-Holdback (Pseudonym über
  zwei Chunks) · Tool-Arg-Reversal · Scope-Isolation · TTL-Ablauf.

---

## Harte Leitplanken

- **Nur an diesem Repo arbeiten.** Temper/Crate/PrismClaw höchstens **read-only** als Referenz
  fürs Interface-Design ansehen — **nie** im selben Auftrag an ihnen operieren. Konsumenten-
  Migration ist eine spätere, getrennte Phase.
- **Verifier nie lockern**, auch nicht „weil reversibel".
- **Versionierte Schnittstelle ab Tag 1** (`/v1/…`) — Sluice ist eine Abhängigkeit mit Vertrag;
  ein Breaking Change trifft sonst alle Konsumenten gleichzeitig.
- **Grenze Mechanismus/Domäne (oben) nicht selbst raten** — gegen die Spec bauen, im Zweifel fragen.
