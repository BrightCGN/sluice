# Claude-Code-Auftrag — Sluice, Phase 1 + 2 (Kern + Modi)

> **Scope dieses Auftrags:** NUR den Sanitisierungs-Kern und die zwei Modi als
> testbare Library bauen — **vor** jeder Netzwerk-Oberfläche. HTTP-Endpoints (Phase 3),
> Konsumenten-Migration (Phase 4) und Aufräumen (Phase 5) sind **explizit nicht Teil dieses
> Auftrags**.
> **Bindend:** `docs/SLUICE-BOUNDARY-SPEC.md` (Wahrheitsquelle) und `CLAUDE.md` (Leitplanken).
> **Prinzip:** Extraktion aus laufendem Code, kein Neubau — Verhalten erhalten, Tests grün.

---

## 0. Quellmaterial

**Portieren (das ist die Extraktionsmasse):**
- Aus **Temper** `src/temper/egress/` → `guard.py`, `policy.py`, `verifier.py` +
  `tests/test_egress_guard.py`. Das ist der reifste Egress-Schnitt (Gate/Verifier/Audit,
  fail-closed). Wird der **generalisierende** Zweig + der universelle Verifier.
- Aus **PrismClaw** `prismclaw.anon` (verwendet in `backend/claude/prismclaw/core/anthropic_client.py`)
  + zugehörige `anon`-Tests + `docs/PRISMCLAW-ANONYMIZATION.md`. Das ist die Referenz für den
  **pseudonymisierenden** Zweig: `forward_messages`, `reverse_text`, `stream_reverser` (Holdback),
  `reverse_obj` (Tool-Args), `scope`.

**Nur read-only als Referenz, NICHT anfassen:** Temper `models/frontier.py` + `models/providers/client.py`
und PrismClaws `gateway/gateway/providers/client.py` (Routing/Failover — bleibt beim Gateway,
kein Sluice-Thema).

---

## 1. Phase 1 — Kern (Mechanismus)

Baue die geguardete Kette. Der Schalter verändert diese Schichten nie (CLAUDE.md, drei Invarianten).

- **`sluice/guard.py`** — `guarded_egress(...)`: Profil-Gate → `mode.forward()` → Verifier →
  Audit. Gibt ein `EgressOutcome(released, sanitized_text, reason)` zurück. Struktur aus Tempers
  `guard.py` übernehmen, aber Modus-Aufruf einziehen (statt fest generalisierend).
- **`sluice/policy.py`** — `check_egress_allowed(profile, purpose)` + das **Profil-Schema** aus
  Spec §4 laden (TOML): `mode` (Alias `strategy`), `egress_enabled`, `allowed_purposes`, `provider_allowlist`,
  `detector_profile`, und für pseudonymizing der `[reversible]`-Block (`scope`, `ttl_seconds`,
  `storage`). Souveränes Profil (`egress_enabled=false`) → alles zu. Default-Deny bei fehlendem
  Profil.
- **`sluice/verifier.py`** — `verify_no_identifiers(text, detector_profile)`. Engine aus Tempers
  `verifier.py` **im Kern unverändert** (fail-closed, konservative Deny-Muster). Neu: die Muster
  kommen aus dem gewählten Detektor-Profil (§2 unten), nicht hart verdrahtet.
- **`sluice/detectors/`** — Muster-Sets als Profile: `infra` (= Tempers heutiges Set: IPv4,
  E-Mail, `*.internal/.local/.corp/.lan/.intra`, FQDN, `/home/<user>`, Secret), `code`
  (infra + interne Repo-/Package-Namen, Env-Werte), `media` (leicht), `financial` (IBAN/BIC,
  Kontonummern, strenger — Muster-Gerüst genügt, Feinschliff ist spätere Profil-Arbeit).
- **`sluice/audit.py`** — `egress_log`-Writer, append-only. Pro Eintrag: `timestamp, profile,
  purpose, mode, released, reason, before(raw), after(sanitized), provider_target,
  verifier_findings[]`. Löst Tempers `TODO(post-v1)` in `_log_egress` ein. v1: strukturiertes
  Logging + in-memory Append-Store; Persistenz ist post-v1.

---

## 2. Phase 2 — Modi

- **`sluice/modes/__init__.py`** — `Mode` (Protocol, Spec §3) mit
  `reversible: bool`, `forward()`, `reverse_text()`, `stream_reverser()`, `reverse_obj()`.
  Plus eine Auswahl-Funktion (`select_mode`), die aus `profile.mode` die Implementierung zieht.
- **`sluice/modes/generalizing.py`** — `reversible=False`. `forward()` verifiziert den vom
  Konsumenten gelieferten *generalisierten* Text (die Generalisierung selbst macht der Konsument —
  NICHT hier, CLAUDE.md/Mechanismus-vs-Domäne). Reverse-Methoden → `NotImplementedError`.
- **`sluice/modes/pseudonymizing.py`** — `reversible=True`. Vollständig aus PrismClaws `anon`:
  - `forward()` / forward über Message-Liste — konsistentes Pseudonym pro Roh-Wert **innerhalb
    des Scope**.
  - `reverse_text()` — Rückweg.
  - `stream_reverser()` — **Holdback-Puffer**: ein Pseudonym kann über zwei Chunks reichen
    (kritischer Failure-Mode Streaming-Passthrough).
  - `reverse_obj()` — Tool-Call-Argumente vor Ausführung zurückmappen (kritischer Failure-Mode
    Tool-Call-Passthrough).
  - **Mapping-Lebenszyklus** (Spec §8): `scope` (session-gebunden), `ttl_seconds`, `storage="memory"`
    (v1), deterministisches Aufräumen bei Scope-Ende, keine Leaks über Scopes.

---

## 3. Definition of Done (Tests)

Kein Netz; wo HTTP-Form nötig, `httpx.MockTransport`. Referenz-Suiten wandern mit als Sicherheitsnetz.

Pflicht-Fälle:
- [ ] **Verifier gleich streng in beiden Modi** — derselbe roh-durchgeschmuggelte Identifier wird
      sowohl bei `generalizing` als auch bei `pseudonymizing` blockiert.
- [ ] **Default-Deny** — Egress ohne deklariertes Profil → nichts raus.
- [ ] **Souveränes Profil** — `egress_enabled=false` → nichts raus, unabhängig vom Modus.
- [ ] **Streaming-Holdback** — Pseudonym, das über zwei SSE-Chunks gesplittet ankommt, wird korrekt
      zurückgemappt.
- [ ] **Tool-Arg-Reversal** — Tool-Argumente kommen in echten Werten bei der (gemockten) Ausführung an.
- [ ] **Scope-Isolation** — zwei Scopes teilen kein Mapping; gleicher Roh-Wert → verschiedene
      Pseudonyme über Scope-Grenzen.
- [ ] **TTL-Ablauf** — Mapping wird nach `ttl_seconds`/Scope-Ende verworfen.
- [ ] **Generalizing wirft** bei Reverse-Aufruf `NotImplementedError`.
- [ ] Portierte Temper- und PrismClaw-Tests laufen grün.

---

## 4. Nicht-Ziele (dieser Auftrag)

- **Keine** HTTP-Endpoints / kein Proxy (`/v1/egress/guard`, `/v1/responses`) — das ist Phase 3.
- **Keine** Änderung an Temper, Crate oder PrismClaw — Konsumenten-Migration ist Phase 4.
- **Keine** semantische Generalisierung in Sluice — das bleibt Konsumenten-Domäne.
- **Kein** Anfassen der Provider-Routing/Failover-Logik.
- **Kein** Genericity-/Re-ID-Check, keine Mapping-Persistenz, kein format-preserving-Modus
  (alles post-v1, Spec §10).

---

## 5. Vorgehen

1. Repo `~/git/sluice` scaffolden (Package `sluice`, `pyproject.toml`, Test-Setup). Prüfen, dass
   der Name im Stack frei ist (Rust-Crate `sluice` existiert, Python-seitig unkritisch).
2. Phase 1 komplett + Tests grün, **bevor** Phase 2 beginnt.
3. Phase 2 komplett + Tests grün.
4. Kurzer Abschlussbericht: was 1:1 portiert, was angepasst wurde und **warum** — besonders jede
   Stelle, an der die Grenze Mechanismus/Domäne eine Entscheidung erzwang. Offene Punkte gegen die
   Spec sammeln (die Praxis wird zeigen, wo die Spec nachgeschärft werden muss).

**Bei Unklarheit über die Mechanismus/Domäne-Grenze: stoppen und fragen, nicht raten.**
