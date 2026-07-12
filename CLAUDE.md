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

Seit **Spec-Revision 9 (2026-07-12)** ist Sluice ein **erweiterbares Egress-Sanitisierungs-
Framework**, kein einzelner *erzwungener* Riegel mehr: **Modi sind eine Registry** (§3),
`passthrough` ist ein First-Class-Modus, die **Modus-Wahl gehört dem Konsumenten**, das
**Logging dem Betreiber**. Sluice ist ein **technischer** Egress-Riegel, **kein Compliance-
Zertifikat** — bewusst *keine* „DSGVO-/BSI-konform"-Zusage (die müsste extern geprüft werden).
Was bleibt: **eine** Policy-Form (Profil), **ein** konfigurierbares Audit-Log, **eine**
Registry austauschbarer Modi über *einem* Interface.

---

## Sichere Defaults & das `strict`-Verhalten (früher: die drei Invarianten)

**Rev. 9 hat die drei „Invarianten" umgedeutet:** sie sind keine *globalen* Invarianten mehr,
sondern das **Verhalten des `strict`-Modus** und der **sicheren Auslieferungs-Defaults**. Der
Modus-**Schalter** wählt jetzt aus einer *Registry*; `passthrough` und schwächere Modi dürfen
abweichen — aber nur nach **expliziter** Konsumenten-Wahl, nie geerbt. Was `strict` (Default)
weiterhin garantiert:

1. **Profil-Gate zuerst.** Kein Profil → nichts raus (Default-Deny). `egress_enabled=false`
   (souverän) → nichts raus, egal welcher Modus. Fehlt das `mode`-Feld → **`strict`**, nie
   `passthrough` (safe by default).
2. **Verifier fail-closed — als Baustein, den `strict`/`full`/`pseudonymizing` komponieren.**
   Der deterministische Riegel (`verify_no_identifiers`) liegt *unter* der Transformation. In
   diesen Modi lockert „reversibel" ihn nie — nur ein *Pseudonym* raus, nie ein roher Wert.
   `passthrough` komponiert ihn bewusst *nicht* (§2.1). **Innerhalb eines Modus, der ihn nutzt,
   wird er nie gelockert.**
3. **Audit — Detailgrad wählt der Betreiber** (`off | metadata | full`, Default `metadata`).
   Wo geschrieben wird: append-only, released *und* blocked. Der Betreiber-Schalter ändert den
   Sanitisierungs-Modus nicht.

---

## Mechanismus vs. Domäne — die Grenze, die du NICHT selbst ziehst

Das ist die zentrale Urteilsentscheidung des Projekts (Spec §1.1). Sie falsch zu ziehen macht
Sluice undicht oder nicht wiederverwendbar. **Nicht selbst raten — gegen die Spec bauen.**

- **In Sluice (generischer Mechanismus):** Profil-Gate, Verifier-Engine, Audit, Diff-Mechanik,
  die Modus-Registry und das Ausführen des gewählten Modus.
- **Beim Konsumenten (Domäne/Policy):** die **semantische Generalisierung** (aus einem
  validierten Fix die übertragbare Lektion machen — das ist Temper-Businesslogik, nicht Sluice),
  die konkreten Detektor-**Muster** (als Profil deklariert), die Provider-Allowlist.
- **Seit Revision 2 in Sluice:** Provider-Adapter (anthropic/openai/gemini/mistral, §7.3)
  inkl. API-Keys per Env — Adapter werden **nur nach `released=true`** aufgerufen, nie davor.
  **Seit Revision 7 verbindlich:** der Kern ruft Provider **nie direkt** — nur über die
  eigenständigen Gateway-Services (`SLUICE_GATEWAY_<PROVIDER>_URL` Pflicht, fehlende URL ⇒
  fail-closed); Provider-Keys liegen ausschließlich bei den Gateways. Jedes Gateway läuft
  unter seinem eigenen System-User `sluice-gw-<provider>` (Rev. 8, User-Isolation).
- **Nicht in Sluice (post-v1 offen):** Failover/Health/Tenant-Order-Routing.

Wenn Domänenlogik „mal eben" nach Sluice greifen will: **das ist das Signal zu stoppen**, nicht
weiterzumachen.

---

## Modus-Registry (Rev. 9)

`Mode` (Protocol, vormals `SanitizationStrategy`) — eine **Registry** benannter Modi,
profil-/request-gewählt; **Dritte registrieren eigene** (Open-Source-Erweiterungspunkt). Jeder
Modus deklariert seine Eigenschaften selbst (`name`, `reversible`) und entscheidet, ob er den
Verifier (§5) komponiert. Eingebaute Modi (initiale Menge, erweiterbar):

- **`strict`** — `reversible=False`, **Auslieferungs-Default**. Transformiert + Verifier
  fail-closed. Trägt die früheren „Invarianten"-Eigenschaften.
- **`passthrough`** — kein Verifier, keine Transformation (§2.1). **Explizites Opt-in.** Für
  bereits nicht-sensible Daten, vertrauenswürdige Ziele, Experimentieren.
- **`generalizing`** — `reversible=False`. `forward()` verifiziert nur den vom Konsumenten
  *bereits generalisierten* Text. Herkunft: Tempers `egress/`-Datenfluss.
- **`pseudonymizing`** — `reversible=True`, **explizites Opt-in** (Profil `mode`/Request
  `mode: "reversible"`). forward + reverse + `stream_reverser` (Holdback) + `reverse_obj` +
  Mapping-Lebenszyklus. Herkunft: PrismClaws `prismclaw.anon`.

Reversibel ist die schwächere Zusage (Mapping-Tabelle bleibt personenbezogen) und führt Zustand
ein — daher **nie geerbt, immer bewusst deklariert**. Ebenso `passthrough`/fail-open: nur nach
expliziter Wahl, optional per `allowed_modes` pro Profil sperrbar (§4.1).

---

## Repo-Struktur (Zielbild)

```
sluice/
  guard.py               # Orchestrierung: Gate → Modus → Verifier → Audit
  policy.py              # Profil-Schema + check_egress_allowed
  verifier.py            # deterministischer Riegel; Muster aus Detektor-Profilen
  audit.py               # egress_log, append-only
  modes/
    __init__.py          # Mode (Protocol) + Registry (select_mode/register_mode)
    strict.py            # auto-redigierend, Auslieferungs-Default (§4.3)
    passthrough.py       # Identität, kein Verifier (§2.1)
    generalizing.py      # forward-only
    pseudonymizing.py    # forward + reverse + stream_reverser + reverse_obj + scope/TTL
  detectors/             # Muster-Sets: infra, code, media, financial
  dispatch.py            # guarded_completion: Guard → Provider-Adapter → (reverse)
  server.py              # Kern-Service: /v1/egress/guard + /v1/chat/completions
  gateway.py             # eigenständiger Gateway-Service, ein Prozess pro Provider (Rev. 6, Ports ab 17890)
  providers/
    __init__.py          # ProviderAdapter (Protocol) + Registry, Keys per Env
    anthropic.py         # Claude (Messages-API)
    openai_compat.py     # gemeinsamer Chat-Completions-Dialekt
    openai.py            # OpenAI
    mistral.py           # Mistral AI
    gemini.py            # Google Gemini (generateContent)
    remote.py            # Kern → Gateway-Service (SLUICE_GATEWAY_<P>_URL, Rev. 6)
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
- **Innerhalb eines Modus, der den Verifier komponiert (`strict`/`full`/`pseudonymizing`), nie
  lockern** — auch nicht „weil reversibel". Ob ein Modus ihn überhaupt komponiert, ist Modus-
  Entscheidung (`passthrough` tut es nicht); der *Auslieferungs-Default bleibt `strict`* mit
  Verifier fail-closed (Rev. 9, §4.3/§5).
- **Versionierte Schnittstelle ab Tag 1** (`/v1/…`) — Sluice ist eine Abhängigkeit mit Vertrag;
  ein Breaking Change trifft sonst alle Konsumenten gleichzeitig.
- **Grenze Mechanismus/Domäne (oben) nicht selbst raten** — gegen die Spec bauen, im Zweifel fragen.
