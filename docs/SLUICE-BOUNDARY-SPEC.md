# Sluice — Boundary- & Contract-Spec (v1)

> **Stand:** 2026-07-09. **Revision 7:** Gateway-Pflicht — der Kern erreicht Provider
> **ausschließlich** über die Gateway-Services; der direkte Adapter-Fallback aus
> Revision 6 entfällt. Fehlende `SLUICE_GATEWAY_<PROVIDER>_URL` ⇒ fail-closed
> Konfigurationsfehler (§7.3). Der Kern hält keine Provider-Keys mehr (Key-Isolation).
> **Revision 6:** Provider-Gateways sind **eigenständige
> Services** (`sluice/gateway.py`, ein Prozess pro Provider, Ports ab **17890**),
> jederzeit auf getrennte Server umziehbar; der Kern dispatcht über
> `SLUICE_GATEWAY_<PROVIDER>_URL` (§7.3) — immer erst nach `released=true`.
> **Revision 5:** optionaler Provider-Lock `SLUICE_PROVIDER` für Kern-Instanzen (§7.3),
> additive Schranke, Invarianten unverändert.
> **Revision 4:** Default-Modus zurück auf **generalizing
> (irreversibel)** — safety first; die Default-Umkehr aus Revision 3 ist rückgängig.
> Pseudonymizing bleibt explizites Opt-in (per Profil oder Request-`mode`, §2/§7.2).
> **Revision 3:** Sluice läuft als eigenständiger HTTP-Service (`sluice/server.py`, §7).
> **Revision 2:** Provider-Kommunikation wandert von PrismClaw-Gateway
> nach Sluice (§1, §1.1, §7.3) — bewusste Design-Änderung.
> Erstfassung 2026-07-07, abgeleitet aus dem Seite-an-Seite-Vergleich der
> zwei realen Referenzimplementierungen: Temper `src/temper/egress/{guard,policy,verifier}.py`
> (irreversibel/generalisierend) und PrismClaw `prismclaw.anon` in
> `backend/claude/prismclaw/core/anthropic_client.py` (reversibel/pseudonymisierend).
> **Bezug:** Temper-Brief §5.14 (Egress-Guard), §5.8 (Modell-Routing), Prinzip 11/13/16
> (Souveränität, append-only Audit, Egress technisch unterbunden).
> **Nachbarn:** `openclaude/docs/PRISMCLAW-GATEWAY.md` (Routing/Failover — bleibt getrennt),
> `openclaude/docs/PRISMCLAW-ANONYMIZATION.md` (Herkunft der reversiblen Strategie).
> **Status jedes Abschnitts:** normativ, sofern nicht als *(offen)* / *(post-v1)* markiert.

---

## 1. Was Sluice ist — und was nicht

Sluice ist die **eine gemeinsame Sanitisierungs-Boundary**, durch die *jeder* ausgehende
Datenpfad aller Projekte läuft, bevor er die Kundengrenze überquert. Es ist der Ort, auf den
bei einer DSGVO-Prüfung gezeigt wird: **eine** Policy, **ein** append-only Audit-Log, **ein**
deterministischer Riegel.

Sluice ist ein **eigenständig deploybarer Dienst** mit eigenem Repo und eigenem
Lebenszyklus (Security-Takt), **kein** in Temper/PrismClaw eingebettetes Modul. Konsumenten
binden es über ein **dünnes Client-SDK** ein (die einzige geteilte Library — kapselt Auth,
Protokoll, Retry).

**Sluice spricht die Provider direkt an (Revision 2):**

```
Konsument (im Perimeter)
   │
   ▼
┌──────────────────────────────────────────────┐
│  SLUICE  (Sanitisierungs-Boundary, Perimeter)│
│   • Profil-Gate      (§4)                     │
│   • Strategie        (§3  generalize|pseudon.)│
│   • Verifier         (§5  harter Riegel)      │
│   • Audit            (§6  egress_log)          │
│   • Provider-Adapter (§7.3, nach dem Riegel)  │
│      ▲ reverse (nur pseudonymisierender Modus)│
└──────┬────────────┬────────────┬─────────────┘
       │ nur sanitisierter Text (Invariante 2)
   ┌───▼───┐   ┌────▼────┐   ┌───▼────┐   ┌────────┐
   │Claude │   │ OpenAI  │   │ Gemini │   │Mistral │  …
   └───────┘   └─────────┘   └────────┘   └────────┘
```

**Souveränitäts-Story (Revision 2, bewusst festgehalten):** Ursprünglich sollte Sluice nur
sanitisieren und das PrismClaw-Gateway routen. Mit Revision 2 übernimmt Sluice die
Provider-Kommunikation selbst — der Provider-Aufruf liegt damit *hinter* Gate, Strategie,
Verifier und Audit im selben Prozess. Die zentrale Zusage bleibt unverändert: **kein
Provider-Adapter wird je mit unverifiziertem Text aufgerufen**; der Adapter ist die letzte
Schicht der Kette, nicht ein Bypass daran vorbei. Der Trade-off dieser Änderung ist ebenso
festgehalten: Sluice trägt jetzt Provider-API-Keys und Erreichbarkeits-Verantwortung
(vorher Gateway-Sache); Routing-Feinheiten wie Tenant-Order/Priority-Failover/Health sind
*(post-v1)* und werden nur bei Bedarf nachgezogen.

### 1.1 Verantwortungs-Schnitt (Mechanismus vs. Policy)

| Zuständigkeit | Sluice (Mechanismus, einmal gebaut) | Konsument (Policy/Domäne, pro Projekt) |
|---|---|---|
| Profil-Gate, Verifier-Engine, Audit, Diff-Mechanik | ✅ | — |
| Strategie-Auswahl ausführen | ✅ | — (wählt nur via Profil) |
| **Semantische Generalisierung** (validierter Fix → übertragbare Lektion) | — | ✅ Temper-Businesslogik |
| Detektor-**Muster** (welche Identifier für diese Domäne) | Engine ✅ / Muster als Profil | ✅ deklariert Profil |
| Provider-**Kommunikation** (Adapter Claude/OpenAI/Gemini/Mistral) | ✅ (§7.3, Revision 2) | — (wählt Provider via Allowlist §4.1) |
| Upstream-Provider-API-Keys | ✅ per Env, nie im Profil/Audit (§7.3) | — |
| Failover/Health/Tenant-Order | *(post-v1)* | — |

**Kernregel:** Sluice garantiert *„kein Identifier überquert die Grenze" + Audit* — projekt­unabhängig.
Die *semantische* Generalisierung bleibt beim Konsumenten. Zieht man Domänenlogik nach Sluice,
muss es jede Konsumenten-Semantik kennen — und man baut es doch wieder jedes Mal neu.

---

## 2. Der zentrale Befund: zwei Strategien, ein Schalter

Der Vergleich der zwei Referenzimplementierungen zeigt **keinen Reifegefälle, sondern zwei
funktionsgetriebene Gegensätze** — beide berechtigt:

| | **Generalizing** (aus Temper) | **Pseudonymizing** (aus PrismClaw) |
|---|---|---|
| Richtung | Einbahnstraße (forward) | Kreis (forward + reverse) |
| DSGVO | irreversibel → aus Scope | Mapping-Tabelle → bleibt personenbezogen |
| Rückweg | keiner | vollständig (Stream-Reverser, Tool-Arg-Reversal) |
| Zustand | zustandslos | session-scoped Mapping-Tabelle (TTL, Scope) |
| Antwort ist | Ratschlag, lokal angewandt (Konfidenz gedeckelt) | direkt nutzbar (echte Werte zurück) |
| Primärer Fall | Temper, Crate, Bank-Tool | Aider / Code-Dogfooding |
| Integrationsform (§7) | konsument-vermittelt (Guard-Call) | proxy-vermittelt (transparenter Endpoint) |

Der **Schalter** wählt eine *Strategie-Implementierung*, kein `if reversible:` im Guard:

```
SanitizationStrategy (Interface)
  ├─ GeneralizingStrategy    → forward() nur; kein Reverse-Leg          [DEFAULT, Rev. 4]
  └─ PseudonymizingStrategy  → forward() + reverse() + stream_reverser
                               + tool_arg_reversal + Mapping-Lebenszyklus
```

**Drei Invarianten, die der Schalter NIE verändert** (er tauscht nur das Strategie-Objekt):

1. **Profil-Gate** (§4) läuft immer zuerst.
2. **Verifier** (§5) läuft in *beiden* Modi **gleich streng** — er ist die Schicht *unter*
   der Strategie, nicht Teil von ihr. „Reversibel" ist **kein** Grund, den Riegel zu lockern:
   auch dann darf nur ein *Pseudonym* raus, nie ein echter Wert, den das Mapping übersah.
3. **Audit** (§6) protokolliert jeden Durchlass append-only, reviewbares Vorher/Nachher.

**Default & Opt-in (Revision 4, safety first):** `GeneralizingStrategy` (irreversibel) ist
der **Default** — die stärkere DSGVO-Zusage: es entsteht keine personenbezogene
Mapping-Tabelle, nichts ist rückrechenbar. `PseudonymizingStrategy` ist die **explizite
Opt-in-Ausnahme**, wählbar per Profil (`strategy = "pseudonymizing"`) oder per Request
(`mode: "reversible"`, §7.2) — nie durch Vererbung, nur durch bewusste Deklaration.
(Historie: Revision 3 hatte den Default kurzzeitig umgekehrt; Revision 4 nimmt das zurück.)
Unverändert in beiden Modi: Verifier gleich streng (Invariante 2), Profil-Gate/Default-Deny
(§4.3), v1-Mapping-Storage nur `memory` mit TTL/Scope-Aufräumen (§8).

---

## 3. Das Strategie-Interface (Mechanismus-Kern)

```python
class SanitizationStrategy(Protocol):
    reversible: bool

    async def forward(self, payload: EgressPayload, scope: Scope | None) -> Sanitized:
        """Roh → sanitisiert (generalisiert ODER pseudonymisiert). Egress-Kandidat."""

    async def reverse_text(self, text: str, scope: Scope) -> str:
        """Sanitisiert → roh. NUR reversible=True. Sonst NotImplementedError."""

    def stream_reverser(self, scope: Scope) -> StreamReverser:
        """Holdback-Puffer: ein Pseudonym kann über zwei Chunks reichen. NUR reversible=True."""

    async def reverse_obj(self, obj: dict, scope: Scope) -> dict:
        """Tool-Call-Argumente zurückmappen. NUR reversible=True."""
```

- **`GeneralizingStrategy`**: `reversible=False`. `forward()` verifiziert nur, dass der vom
  Konsumenten gelieferte *generalisierte* Text egress-tauglich ist (die eigentliche
  Generalisierung macht der Konsument, §1.1). Reverse-Methoden werfen `NotImplementedError`.
  Herkunft: Tempers Datenfluss (`guard.py` ruft nur `verify_no_identifiers`).
- **`PseudonymizingStrategy`**: `reversible=True`. Volle Implementierung aller vier Methoden.
  Herkunft: PrismClaws `anon` (`forward_messages`, `reverse_text`, `stream_reverser` mit
  Holdback, `reverse_obj` für Tool-Args). Trägt den Mapping-Lebenszyklus (§8).

Ein späteres drittes Verfahren (z. B. **format-preserving** fürs Bank-Tool: Beträge/IBANs
strukturerhaltend maskieren) ist einfach eine dritte `SanitizationStrategy` hinter demselben
Schalter — **ohne** Guard, Verifier oder Audit anzufassen. *(post-v1)*

---

## 4. Profil-Schema (der Schalter, maschinenlesbar)

Jeder Konsument deklariert **ein Profil**. Das Profil ist die vollständige, auditierbare Form
seiner Egress-Erlaubnis.

```toml
[profile."temper"]
strategy            = "generalizing"          # = Default (Rev. 4); Angabe optional
egress_enabled      = true                    # false = souverän/air-gapped: NICHTS raus (§4.2)
allowed_purposes    = ["external_escalation", "promotion_upload"]
provider_allowlist  = ["claude", "gemini"]    # welche Provider dieses Profil überhaupt darf (§4.1)
detector_profile    = "infra"                 # welches Detektor-Set der Verifier lädt (§5.1)

[profile."aider-code"]
strategy            = "pseudonymizing"        # explizites Opt-in (Rev. 4)
egress_enabled      = true
allowed_purposes    = ["code_completion"]
provider_allowlist  = ["claude"]
detector_profile    = "code"
  [profile."aider-code".reversible]
  scope             = "session"               # Mapping-Scope (§8)
  ttl_seconds       = 3600
  storage           = "memory"                # memory | persistent(post-v1)

[profile."crate"]
strategy            = "generalizing"
egress_enabled      = true
allowed_purposes    = ["playlist_curation"]
provider_allowlist  = ["claude", "gemini", "openai"]
detector_profile    = "media"

[profile."bank-tool"]                         # strengstes Profil (§4.4)
strategy            = "generalizing"
egress_enabled      = true
allowed_purposes    = ["strategy_reasoning"]
provider_allowlist  = ["claude-zdr"]          # nur Zero-Retention / lokal
detector_profile    = "financial"
```

### 4.1 Provider-Allowlist pro Profil
Weil Modell-Adapter generisch sind (§7.3), unterscheidet sich die *Erlaubnis* pro Profil —
Provider divergieren in Retention/Training. Bank-Profil ⇒ nur ZDR/lokal; Crate darf großzügiger.

### 4.2 `egress_enabled = false` = souveränes Profil
Maschinenlesbare Form von Prinzip 16 (Tempers `policy.py`). Guard lässt **nichts** durch,
egal welche Strategie — der Riegel greift *vor* der Strategie-Auswahl.

### 4.3 Default-Deny
Ein Konsument **ohne** Profil bekommt **nichts** raus. Kein Vererben fremder Profile.
Neue Projekte erben nie versehentlich Crates lockere Policy.

### 4.4 Mandanten-Isolation
Geteilter Code, aber getrennte Policies, Audit-Streams, Credentials und Budgets **pro Profil**.
Ein Bug/eine schlampige Regel in `crate` darf niemals `temper`- oder `bank-tool`-Daten mitreißen.

---

## 5. Der Verifier (universeller, deterministischer Backstop)

Herkunft: Tempers `verifier.py`. **Läuft in beiden Modi, gleich streng.** Fail-closed:
findet er *irgendeinen* rohen Identifier → **blockiert** (`clean=False`). Lieber false-positive
als ein Leck. Er ist der harte Riegel *unter* der probabilistischen Strategie.

**Wichtig für die Konsolidierung:** PrismClaws Pfad pseudonymisiert heute, hat aber **kein
unabhängiges** „ist wirklich kein roher Identifier durchgerutscht?"-Gate vor dem Absenden.
Dieser Backstop ist Tempers Beitrag zur gemeinsamen Schicht — und **muss auch den reversiblen
Modus absichern**.

### 5.1 Detektor-**Profile** (Muster pro Domäne, Engine geteilt)
Die *Engine* (Regex/NER-Runner) ist geteilt; die *Muster* kommen aus dem `detector_profile`:

| Profil | Beispiel-Muster (nicht abschließend) |
|---|---|
| `infra` | IPv4, E-Mail, `*.internal/.local/.corp/.lan/.intra`, FQDN, `/home/<user>`, Secret/Token — **= Tempers heutiges Set** |
| `code` | wie `infra` + interne Package-/Repo-Namen, Pfade, Env-Var-Werte |
| `media` | (leichter) Pfade, NAS-Hosts; kaum PII |
| `financial` | IBAN/BIC, Kontonummern, Beträge?*, Gegenparteien-Namen — **strenger justiert** |

\* Beim Bank-Tool widersprechen sich Sanitisierung und Lösbarkeit maximal (Beträge *sind* der
Nutzen). Auflösung: Rechnen bleibt **lokal**; nur die abstrahierte Strategiefrage (Kategorien/
Spannen statt Salden) geht raus. Das ist Profil-Arbeit, kein Sluice-Mechanismus. *(Details post-v1)*

### 5.2 Genericity-Check (Re-Identifikation durch Kombination) *(post-v1)*
Reifeversion: nicht nur „enthält Identifier?", sondern „generisch genug, um aus vielen
Umgebungen zu stammen?". Justierbar pro Profil (bank-tool strenger als temper).

---

## 6. Audit (`egress_log`, append-only)

Genau *ein* Log, das der Guard bei **jedem** Durchlass schreibt — released *und* blocked.
Löst Tempers heutiges `TODO(post-v1)` in `guard.py::_log_egress` in Sluice ein.

Pro Eintrag: `timestamp, profile, purpose, strategy, released(bool), reason, before(raw),
after(sanitized), provider_target, verifier_findings[]`. Das **reviewbare Vorher/Nachher** ist
die Fläche fürs Admin-Gate und der DSGVO-/Audit-Nachweis. Append-only (Prinzip 13).

---

## 7. Der Vertrag: Konsument ↔ Sluice

Der Schalter wählt nicht nur einen Algorithmus, sondern eine **Integrationsform**. Beide
Formen münden in denselben geguardeten Kern (Profil-Gate → Strategie → Verifier → Audit).

### 7.1 Generalizing = konsument-vermittelt (Guard-Call-API)
Der Konsument besitzt die semantische Generalisierung (Domänenlogik). Er ruft Sluice als
**Gate**, nicht als Proxy. Entspricht Tempers heutigem `guarded_egress`.

```
POST /v1/egress/guard
{ "profile":"temper", "purpose":"external_escalation",
  "raw_text":"…konkret (nur Audit)…", "generalized_text":"…egress-Kandidat…" }

→ 200 { "released":true,  "sanitized_text":"…", "reason":"clean" }
→ 200 { "released":false, "sanitized_text":null, "reason":"Verifier blockiert: IP-Adresse: …" }
```
Bei `released=true` dispatcht Sluice `sanitized_text` selbst über den Provider-Adapter (§7.3);
alternativ kann der Konsument nur den Guard nutzen und selbst dispatchen (Guard-only-Call).
Blockt der Guard → lokale, eskalierte Nicht-Diagnose (Mensch übernimmt).

### 7.2 Pseudonymizing = proxy-vermittelt (transparenter Endpoint)
Sluice sitzt **inline** als OpenAI-/Responses-kompatibler Endpoint. Der Konsument (Aider)
zeigt einfach auf Sluices URL und weiß nichts von Sluice. Sluice: pseudonymisiert die ganze
Anfrage → verifiziert → ruft den Provider-Adapter (§7.3) → **reverst den Antwort-Stream**
(inkl. Tool-Arg-Reversal). Entspricht PrismClaws heutigem `anon`-im-Client, herausgehoben.

```
POST /v1/chat/completions
  Header: X-Sluice-Profile: aider-code
          X-Sluice-Scope:   <session-id>       (optional; Mapping-Scope §8, default "global")
  Body:   { "messages":[…echte Daten…], "model":"…", "provider":"…", "stream":true,
            "mode":"reversible" }

`mode` (optional): "irreversible" (Default, Rev. 4) | "reversible" (explizites Opt-in) —
überschreibt die Profil-Strategie für diesen Request. `provider` (optional): default ist der
erste Eintrag der Profil-Allowlist; jede Wahl wird gegen die Allowlist geprüft (§4.1).

Sluice-intern:  forward(scope) → verify → Provider-Adapter (§7.3) → stream_reverser(scope) → Tool-Args reverse
→ SSE zurück an den Konsumenten in **echten Werten**.
```

**Die zwei kritischen Failure-Modes (must-pass, aus dem Aider-Dogfooding):**
1. **Streaming-Passthrough** — ein Pseudonym kann über zwei SSE-Chunks reichen ⇒ Holdback-Puffer
   im `stream_reverser` (PrismClaw hat die Referenz).
2. **Tool-Call-Passthrough** — Tool-Argumente müssen vor Ausführung zurückgemappt werden
   (`reverse_obj`), sonst bekommt das lokale Tool Pseudonyme statt echter Werte.

### 7.3 Provider-Adapter in Sluice (Revision 2)
Sluice spricht die Provider direkt an. Ein Adapter pro Provider hinter demselben
Verifier/Gate; neuer Provider = ein weiterer Adapter, ohne Guard/Verifier/Audit anzufassen.
Die Allowlist (§4.1) begrenzt pro Profil, welche erlaubt sind.

- **v1-Adapter:** `anthropic` (Claude), `openai`, `gemini`, `mistral`. OpenAI und Mistral
  teilen den Chat-Completions-Dialekt (gemeinsame Basis-Klasse).
- **Reihenfolge zwingend:** Der Adapter wird ausschließlich vom Dispatch aufgerufen,
  *nachdem* `guarded_egress` released hat — nie mit unverifiziertem Text (Invariante 2).
- **API-Keys:** per Env-Variable (`SLUICE_ANTHROPIC_API_KEY`, `SLUICE_OPENAI_API_KEY`,
  `SLUICE_GEMINI_API_KEY`, `SLUICE_MISTRAL_API_KEY`) — seit Revision 7 **nur in der Env
  des jeweiligen Gateway-Prozesses**, nie beim Kern. Nie im Profil-TOML, nie im Audit-Log.
  Fehlender Key ⇒ fail-closed (Fehler, kein stiller Fallback auf anderen Provider).
- **Timeouts:** endlicher Connect-Timeout, **kein Read-Timeout** (agentische Turns streamen
  lange) — wie im übrigen Code.
- **Antwortpfad:** bei `pseudonymizing` läuft die Provider-Antwort durch `reverse_text` /
  `stream_reverser` / `reverse_obj` derselben Strategie-Instanz (Scope-Konsistenz, §8).
- **Failover/Health/Tenant-Order:** *(post-v1)* — v1 ruft genau den einen per Profil
  erlaubten und vom Konsumenten gewählten Provider.
- **Eigenständige Gateway-Services (Revision 6):** jedes Provider-Gateway ist ein
  **eigener Service** (`sluice/gateway.py`, systemd-Template `deploy/sluice-gateway@.service`,
  Ports ab **17890**: anthropic 17890, openai 17891, gemini 17892, mistral 17893) mit eigenem
  Lebenszyklus — Kern und Gateways können jederzeit auf **getrennte Server** umziehen; der
  Kern kennt ein Gateway nur über `SLUICE_GATEWAY_<PROVIDER>_URL` und braucht dann selbst
  **keinen Provider-Key** (Key-Isolation: jedes Gateway hält nur seinen eigenen).
  Interner Vertrag Kern → Gateway (versioniert, §7.4): `POST /v1/complete`
  (messages/model/max_tokens[/stream] → `{text, model, provider}` bzw. SSE
  `data: {"delta": …}` + `[DONE]`), `GET /v1/health`.
  **Die Boundary bleibt im Kern:** ein Gateway wird ausschließlich vom Dispatch aufgerufen,
  *nachdem* `guarded_egress` released hat — es sieht nie Rohtext und ist **nie direkt von
  Konsumenten erreichbar** (Firewall: eingehend nur vom Sluice-Kern; optional Shared Secret
  `SLUICE_GATEWAY_TOKEN` auf beiden Seiten).
- **Gateway-Pflicht (Revision 7, verbindlich):** Der Kern ruft Provider **nie direkt** —
  `select_egress_adapter` kennt ausschließlich Gateway-Adapter. Fehlt die
  `SLUICE_GATEWAY_<PROVIDER>_URL` des angefragten Providers, ist das ein
  **Konfigurationsfehler** (HTTP 500 `sluice_provider_config`, fail-closed), kein
  stiller Fallback auf den direkten Adapter. Die direkten Adapter (`select_provider`)
  laufen nur noch innerhalb der Gateway-Prozesse. Konsequenz: `sluice.service` allein
  kann keinen Egress zu einem Provider ausführen — pro genutztem Provider muss die
  zugehörige `sluice-gateway@<provider>`-Instanz laufen (Trennung der Lebenszyklen
  ist damit erzwungen, nicht nur möglich).
- **Provider-Lock für Kern-Instanzen (Revision 5, optional):** `SLUICE_PROVIDER` beschränkt
  eine Kern-Instanz auf genau einen Provider (fremde Provider ⇒ 403, fail-closed; Allowlist
  §4.1 gilt unverändert) — zusätzliche Schranke, kein Ersatz für die Gateway-Services.

### 7.4 Versionierte Schnittstelle (ab v1 Pflicht)
Sluice ist ab jetzt eine **Abhängigkeit mit Vertrag** — ein Breaking Change trifft alle
Konsumenten gleichzeitig. `Accept: application/vnd.sluice.v1+json` bzw. `/v1/…`-Pfad-Präfix
und eine Kompatibilitätszusage von Beginn an, sonst wird jedes Update zur Drei-Repo-Migration.

---

## 8. Mapping-Lebenszyklus (nur PseudonymizingStrategy)

Der Zustand, den der reversible Modus einführt und der generalisierende nie hat. Ehrlicher
Zusatzaufwand — der Schalter macht die *Auswahl* billig, nicht die Strategie selbst.

- **Scope:** session-gebunden. Gleicher Roh-Wert → gleiches Pseudonym **innerhalb** des Scope
  (Konsistenz über den Dialog), verschiedene Scopes teilen kein Mapping.
- **TTL:** `ttl_seconds` pro Profil; Mapping wird nach Ablauf/Session-Ende verworfen.
- **Storage:** v1 `memory` (in-process, pro Session). `persistent` *(post-v1)* — mit dem
  bewussten DSGVO-Hinweis, dass eine persistierte Mapping-Tabelle personenbezogen bleibt.
- **Aufräumen:** deterministisch bei Scope-Ende; kein Leak über Sessions hinweg (Isolation §4.4).

Herkunft: PrismClaws `anon.scope(...)` + Holdback-Puffer im Streaming-Reverser.

---

## 9. Extraktion/Konsolidierung — Ausführungsplan für Claude Code

**Reihenfolge zwingend: erst Sluice bauen (aus den zwei Referenzen), dann Konsumenten einzeln
migrieren. Nicht drei Repos in einem Rutsch.** Verhalten erhalten, bestehende Tests grün.

**Phase 0 — Scaffold.** Neues Repo `~/git/sluice`, CLI/Package `sluice`. Prüfen, dass der Name
im Stack frei ist (es gibt eine Rust-Crate `sluice`; im Python-/Homelab-Umfeld unkritisch).

**Phase 1 — Kern (Mechanismus).**
- `sluice/guard.py` ← Tempers `egress/guard.py` (Orchestrierung: Gate → Strategie → Verifier → Audit).
- `sluice/policy.py` ← Tempers `egress/policy.py`, erweitert um das Profil-Schema (§4).
- `sluice/verifier.py` ← Tempers `egress/verifier.py` **unverändert im Kern**; Muster in
  Detektor-Profile (§5.1) ausgelagert.
- `sluice/audit.py` ← `egress_log`, append-only (§6) — löst Tempers TODO ein.

**Phase 2 — Strategien.**
- `sluice/strategies/generalizing.py` ← forward-only (aus Tempers Datenfluss).
- `sluice/strategies/pseudonymizing.py` ← PrismClaws `prismclaw.anon`: `forward_messages`,
  `reverse_text`, `stream_reverser` (Holdback), `reverse_obj`, `scope`/TTL (§8).
- `SanitizationStrategy`-Interface (§3); Auswahl über Profil.

**Phase 3 — Schnittstellen.**
- `/v1/egress/guard` (§7.1) und der Proxy-Endpoint `/v1/responses` + `/v1/chat/completions` (§7.2).
- Dünnes Client-SDK (`sluice-client`) — die *eine* erlaubte geteilte Library.

**Phase 4 — Konsumenten migrieren (einzeln, je mit grünen Tests).**
- **PrismClaw:** `anon` aus `core/anthropic_client.py` **entfernen** (ist jetzt Sluices Job,
  davor); Provider-`client.py` (Routing/Failover) **unangetastet**. Anthropic-Client sieht nur
  noch sanitisierten Text. Safety-Netz: bestehende `anon`-Tests wandern nach Sluice.
- **Temper:** hört auf, selbst zu guarden; `models/frontier.py` ruft Sluice statt `egress/guard.py`.
  Safety-Netz: `tests/test_egress_guard.py` als Referenz nach Sluice übernehmen.
- **Crate:** bindet Sluice als *neuen* Konsument an (echte Call-Sites entstehen erst hier).

**Phase 5 — Aufräumen.** Repo-Pfade weg von der `openclaude`/Carapace-Altlast; `grep -ri
prismclaw ~/git/` gegen die Namenskollision (Prismclaw war Tempers alter Arbeitstitel).

**Leitplanke für Claude Code:** Die **Grenzziehung Mechanismus/Domäne (§1.1) nicht selbst
raten.** Extraktion läuft *gegen diese Spec*; im Zweifel Domänenlogik beim Konsumenten lassen.
Read-only-Blick in Temper/Crate erlaubt fürs Interface-Design, aber im selben Auftrag nicht an
ihnen operieren.

---

## 10. Offen / bewusst später

- **Mapping-Persistenz** (§8): v1 in-memory; `persistent` mit DSGVO-Vorbehalt — *(offen)*.
- **Genericity-Check** (§5.2, Re-ID durch Kombination) — *(post-v1)*.
- **Format-preserving Strategy** fürs Bank-Tool als dritte `SanitizationStrategy` — *(post-v1)*.
- **`egress_log`-Persistenz** über reines Logging hinaus — *(post-v1)*.
- **mTLS Konsument ↔ Sluice** als Härtung (heute VLAN-firewalled) — *(post-v1)*.
- **Bank-Tool-Lesezugriff** (FinTS/HBCI/Aggregator) ist eine *separate* Sicherheitsfläche,
  **nicht** Teil von Sluice — nur der Vollständigkeit halber genannt.

---

## Anhang A — Landschaft nach der Konsolidierung

| Projekt | Rolle ggü. Sluice | Strategie | Integrationsform |
|---|---|---|---|
| **Sluice** | *ist* die Boundary | — | — |
| **PrismClaw** | Konsument (verliert eingebackene `anon`; Gateway-Rolle entfällt mit Revision 2, §7.3) | — / pseudonymizing | Proxy (§7.2) |
| **Temper** | Konsument | generalizing | Guard-Call (§7.1) |
| **Crate** | künftiger Konsument | generalizing | Guard-Call (§7.1) |
| **Aider/Code** | Konsument | **pseudonymizing** (Opt-in) | Proxy (§7.2) |
| **Bank-Tool** | künftiger Konsument | generalizing (financial) | Guard-Call (§7.1) |
