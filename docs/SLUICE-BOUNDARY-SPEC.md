# Sluice — Boundary- & Contract-Spec (v1)

> **Stand:** 2026-07-12. **Revision 10:** Profil-**Wörterbuch** (`dictionary_terms`, §4/§5.1) —
> konsument-deklarierte **Literale** (Namen, Adressen), die die generischen Regex-Detektoren
> (`infra`/`media`) prinzipbedingt nicht erkennen. Sie ergänzen das Detektor-Set des Profils:
> `strict` redigiert sie zu `[NAME]`, der Verifier blockt sie **fail-closed in jedem Modus**.
> Mechanismus/Domäne bleibt sauber (§1.1): die *Term-Liste* ist Domäne (im Profil deklariert),
> das *literale Matching* (wortgrenzen-gebunden, case-insensitiv) ist Sluice-Engine. Schließt die
> Deckungslücke für freie Namen — Herkunft ist das `dictionary` aus PrismClaws/Crates anon, jetzt
> zentral. Additiv und rückwärtskompatibel (Default leer). **Revision 9:** Richtungswechsel — Sluice ist ein **erweiterbares
> Egress-Sanitisierungs-Framework**, kein einzelner *erzwungener* Riegel mehr. **Modi sind eine
> Registry** (§3): jeder Modus ist ein eigenständiger Egress-Handler hinter *einem* Interface;
> `passthrough` ist ein First-Class-Modus (§2.1). **Die Modus-Wahl gehört dem Konsumenten**,
> **das Logging dem Betreiber** (Audit konfigurierbar `off | metadata | full`, §6). Konsequenz:
> die früheren „drei Invarianten, die nie verändert werden" sind **keine globalen Invarianten
> mehr** — sie sind jetzt das **Verhalten des `strict`-Modus** und der **sicheren Auslieferungs-
> Defaults** (§2). `verify_no_identifiers`, die Detektor-Muster und das Audit werden von
> globalen Zwangs-Schichten zu **wiederverwendbaren Bausteinen**, die ein Modus komponiert
> (§3.1, §5, §6). **Safe by default:** ausgelieferter Default ist `strict`/deny; `passthrough`
> und fail-open-Modi sind **explizites Opt-in** — ein unkonfigurierter Konsument leakt nie
> aus Versehen (§4.3). **Wording:** raus mit „DSGVO-/BSI-konform" — Sluice ist ein *technischer*
> Egress-Riegel, **kein Zertifikat** (keine Compliance-Zusage, die eine externe Prüfung
> nachweisen müsste). **Open-Source-Ziel:** generischer Kern öffentlich, konkrete
> Profile/Detektor-Muster/Deploy-Configs privates Overlay — dieselbe Mechanismus/Domäne-Grenze
> wie §1.1, jetzt auch als Repo-Grenze (§11, *offen*). Die Revisionen 2–8 bleiben gültig, soweit
> Rev. 9 sie nicht ausdrücklich umdeutet.
> **Revision 8:** User-Isolation — jedes Provider-Gateway läuft
> unter seinem **eigenen System-User** `sluice-gw-<provider>` (systemd `User=sluice-gw-%i`,
> §7.3); kein Gateway kann Dateien/Keys eines anderen oder des Kerns lesen.
> **Revision 7:** Gateway-Pflicht — der Kern erreicht Provider
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
> `openclaude/docs/PRISMCLAW-ANONYMIZATION.md` (Herkunft des reversiblen Modus).
> **Status jedes Abschnitts:** normativ, sofern nicht als *(offen)* / *(post-v1)* markiert.

---

## 1. Was Sluice ist — und was nicht

Sluice ist ein **erweiterbares Egress-Sanitisierungs-Framework** — eine gemeinsame
Sanitisierungs-Boundary, durch die ein ausgehender Datenpfad *läuft, wenn der Konsument sie
wählt*, bevor er die Kundengrenze überquert. Es bündelt an *einem* Ort: **eine** Policy-Form
(Profil), **ein** (konfigurierbares) Audit-Log, **eine** Registry austauschbarer
Sanitisierungs-Modi über *einem* Interface.

Sluice ist ein **technischer** Egress-Riegel, **kein Compliance-Zertifikat** (Rev. 9): es trägt
bewusst *keine* „DSGVO-/BSI-konform"-Zusage, die eine externe Prüfung nachweisen müsste. Was es
liefert, ist ein nachvollziehbarer, wiederverwendbarer *Mechanismus* — welche rechtliche Aussage
ein Betreiber daraus ableitet, ist dessen Sache, nicht Sluices Versprechen.

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
│   • Modus (Registry) (§3  strict|passthrough│ │
│                          |pseudon.|…)         │
│   • Verifier         (§5  Baustein; strict:   │
│                          harter Riegel)       │
│   • Audit            (§6  off|metadata|full)  │
│   • Provider-Adapter (§7.3, nach dem Modus)   │
│      ▲ reverse (nur reversibler Modus)        │
└──────┬────────────┬────────────┬─────────────┘
       │ Text gemäß gewähltem Modus (strict: sanitisiert; passthrough: unverändert)
   ┌───▼───┐   ┌────▼────┐   ┌───▼────┐   ┌────────┐
   │Claude │   │ OpenAI  │   │ Gemini │   │Mistral │  …
   └───────┘   └─────────┘   └────────┘   └────────┘
```

**Souveränitäts-Story (Revision 2, bewusst festgehalten):** Ursprünglich sollte Sluice nur
sanitisieren und das PrismClaw-Gateway routen. Mit Revision 2 übernimmt Sluice die
Provider-Kommunikation selbst — der Provider-Aufruf liegt damit *hinter* Gate, Modus,
Verifier und Audit im selben Prozess. Die zentrale Zusage bleibt unverändert: **kein
Provider-Adapter wird je mit unverifiziertem Text aufgerufen**; der Adapter ist die letzte
Schicht der Kette, nicht ein Bypass daran vorbei. Der Trade-off dieser Änderung ist ebenso
festgehalten: Sluice trägt jetzt Provider-API-Keys und Erreichbarkeits-Verantwortung
(vorher Gateway-Sache); Routing-Feinheiten wie Tenant-Order/Priority-Failover/Health sind
*(post-v1)* und werden nur bei Bedarf nachgezogen.

### 1.1 Verantwortungs-Schnitt (Mechanismus vs. Policy)

| Zuständigkeit | Sluice (Mechanismus, einmal gebaut) | Konsument (Policy/Domäne, pro Projekt) |
|---|---|---|
| Profil-Gate, Verifier-Engine, Audit, Diff-Mechanik, **Modus-Registry** | ✅ | — |
| Modus-Auswahl ausführen (inkl. eigener Dritt-Modi) | ✅ Engine | — (wählt via Profil/Request; kann eigenen Modus registrieren) |
| **Semantische Generalisierung** (validierter Fix → übertragbare Lektion) | — | ✅ Temper-Businesslogik |
| Detektor-**Muster** (welche Identifier für diese Domäne) | Engine ✅ / Muster als Profil | ✅ deklariert Profil |
| Provider-**Kommunikation** (Adapter Claude/OpenAI/Gemini/Mistral) | ✅ (§7.3, Revision 2) | — (wählt Provider via Allowlist §4.1) |
| Upstream-Provider-API-Keys | ✅ per Env, nie im Profil/Audit (§7.3) | — |
| Failover/Health/Tenant-Order | *(post-v1)* | — |

**Kernregel:** Sluice liefert den projektunabhängigen *Mechanismus* — Profil-Gate, Modus-Registry,
Verifier-Engine, Audit. Die *Zusage* „kein Identifier überquert die Grenze" gilt für den
`strict`-Modus (Default); wählt ein Konsument bewusst einen schwächeren Modus, trägt er das Risiko
(§2.1). Die *semantische* Generalisierung bleibt beim Konsumenten. Zieht man Domänenlogik nach
Sluice, muss es jede Konsumenten-Semantik kennen — und man baut es doch wieder jedes Mal neu.

---

## 2. Der zentrale Befund: zwei Modi, ein Schalter

Der Vergleich der zwei Referenzimplementierungen zeigt **keinen Reifegefälle, sondern zwei
funktionsgetriebene Gegensätze** — beide berechtigt:

| | **Generalizing** (aus Temper) | **Pseudonymizing** (aus PrismClaw) |
|---|---|---|
| Richtung | Einbahnstraße (forward) | Kreis (forward + reverse) |
| Personenbezug | irreversibel → aus Scope | Mapping-Tabelle → bleibt personenbezogen |
| Rückweg | keiner | vollständig (Stream-Reverser, Tool-Arg-Reversal) |
| Zustand | zustandslos | session-scoped Mapping-Tabelle (TTL, Scope) |
| Antwort ist | Ratschlag, lokal angewandt (Konfidenz gedeckelt) | direkt nutzbar (echte Werte zurück) |
| Primärer Fall | Temper, Crate, Bank-Tool | Aider / Code-Dogfooding |
| Integrationsform (§7) | konsument-vermittelt (Guard-Call) | proxy-vermittelt (transparenter Endpoint) |

Diese zwei Referenz-Implementierungen sind seit Rev. 9 **zwei Modi unter mehreren in der
Registry** (§3) — Herkunft: Temper → `generalizing`, PrismClaw → `pseudonymizing`. Der
**Schalter** wählt eine *Modus-Implementierung* aus der Registry, kein `if reversible:` im Guard:

```
Mode (Protocol, Registry — §3)          # vormals SanitizationStrategy, Rev. 9 umbenannt
  ├─ strict          → auto-redigiert + Verifier fail-closed; irreversibel   [DEFAULT, §4.3]
  ├─ passthrough     → Identität; kein Verifier, keine Transformation (§2.1)
  ├─ generalizing    → forward() nur; verifiziert konsument-generalisierten Text
  └─ pseudonymizing  → forward() + reverse() + stream_reverser
                       + tool_arg_reversal + Mapping-Lebenszyklus (reversible=True)
```

**Früher „drei Invarianten" — jetzt das Verhalten des `strict`-Modus (Rev. 9).** Bis Rev. 8
galten drei Regeln als global und unabänderlich. Mit der Modus-Registry (§3) sind sie **keine
globalen Invarianten mehr**, sondern die **definierenden Eigenschaften des `strict`-Modus** und
der **sicheren Auslieferungs-Defaults**. Der `strict`-Modus (und alles, was auf ihm aufbaut)
garantiert weiterhin genau dies — deshalb ist er der Default (§4.3):

1. **Profil-Gate** (§4) läuft zuerst.
2. **Verifier** (§5) läuft **fail-closed** — findet er *irgendeinen* rohen Identifier →
   blockiert. Als *Baustein* (§3.1) frei komponierbar; im `strict`-Modus ist er der harte Boden,
   auch im reversiblen Fall („reversibel" lockert ihn *innerhalb* dieses Modus nie: nur ein
   *Pseudonym* raus, nie ein echter Wert, den das Mapping übersah).
3. **Audit** (§6) protokolliert den Durchlass — im `strict`-Modus append-only mit reviewbarem
   Vorher/Nachher. Der *Betreiber* kann den Detailgrad global senken (`off | metadata | full`),
   ohne den Sanitisierungs-Modus zu verändern.

**Was ein anderer Modus tun darf.** `passthrough` (§2.1) lässt den Verifier *aus* und
transformiert nicht; ein `basic`/regex-Modus darf eine kleinere Erkennungsfläche haben; ein
Dritt-Modus darf fail-open sein. Das ist erlaubt, weil es **explizit gewählt** wird — nicht
still geerbt (§4.3). Die frühere Zusage „*jeder* Egress läuft durch den Riegel" wird damit zu
„*der `strict`-Default* läuft durch den Riegel, und Abweichung ist eine bewusste, im Profil/
Request sichtbare Konsumenten-Entscheidung".

**Default & Opt-in (Rev. 9, safety first):** Der **ausgelieferte Default-Modus ist `strict`**
(irreversibel, Verifier fail-closed) — die stärkste Zusage: keine personenbezogene
Mapping-Tabelle, nichts rückrechenbar, kein roher Identifier durch. Alle **schwächeren Modi
sind explizites Opt-in** — `passthrough` (§2.1), ein regex-`basic`-Modus, der reversible
`pseudonymizing`-Modus (wählbar per Profil `mode = "pseudonymizing"` oder per Request
`mode: "reversible"`, §7.2) — **nie durch Vererbung, nur durch bewusste Deklaration** (§4.3).
Die Reversibilität ist eine *Eigenschaft des jeweiligen Modus* (`reversible: bool`, §3), keine
eigene globale Achse mehr. (Historie: Rev. 3 kehrte den Default kurz um, Rev. 4 nahm das zurück,
Rev. 9 verallgemeinert „Strategie" zu „Modus".) Für den reversiblen Modus gilt unverändert:
Verifier komponiert fail-closed, v1-Mapping-Storage nur `memory` mit TTL/Scope-Aufräumen (§8).

### 2.1 `passthrough` — der triviale Modus (Rev. 9)

`passthrough` ist ein **First-Class-Modus**, kein Bypass an der Boundary vorbei: der Payload
läuft durch dieselbe Kette (Profil-Gate → Modus → Audit), aber der `passthrough`-Modus
**transformiert nicht und komponiert den Verifier nicht** — er reicht den Text unverändert an
den Dispatch. Anwendungsfälle: bereits nicht-sensible Daten, lokale/vertrauenswürdige Ziele,
und ausdrücklich **Experimentieren** (eigener Modus in Entwicklung, A/B gegen einen echten
Sanitisierungs-Modus).

Regeln, die auch für `passthrough` gelten:
- **Explizites Opt-in.** Nur erreichbar, wenn das Profil ihn erlaubt bzw. der Request ihn setzt
  — nie der ausgelieferte Default, nie geerbt (§4.3). Ein unkonfigurierter Konsument bekommt
  `passthrough` **nie** aus Versehen.
- **Profil-Gate läuft trotzdem zuerst.** `egress_enabled = false` (souverän) blockiert auch
  `passthrough` — das Gate liegt *vor* der Modus-Auswahl (§4.2).
- **Audit nach Betreiber-Config** (§6): `metadata` protokolliert *dass* ein `passthrough`-Durchlass
  geschah (Profil/Zeit/Modus/Provider) ohne Vorher/Nachher; `off` schaltet auch das ab; `full`
  loggt den Text. Der Betreiber wählt — nicht der Modus.

**Verantwortung wandert zum Konsumenten.** Wer `passthrough` wählt, trägt das Egress-Risiko
selbst; Sluice sagt dann nur noch „das Profil erlaubt es und (je nach Config) hier ist der
Log-Eintrag" zu — nicht mehr „kein roher Identifier ging raus". Das ist die bewusste
Framework-Zusage aus Rev. 9.

---

## 3. Das Modus-Interface (Mechanismus-Kern)

Ein **Modus** ist die Registry-Einheit: ein eigenständiger Egress-Handler hinter *einem*
Interface, adressiert über seinen `name`. Sluice liefert eine Reihe eingebauter Modi; **Dritte
registrieren eigene** (das ist der Open-Source-Erweiterungspunkt, Rev. 9). Jeder Modus deklariert
seine eigenen Eigenschaften — insbesondere `reversible` — statt dass eine globale Achse sie
vorgibt.

```python
class Mode(Protocol):                       # vormals SanitizationStrategy (Rev. 9 umbenannt)
    name: str                               # Registry-Schlüssel, z. B. "strict", "passthrough"
    reversible: bool                        # Eigenschaft DES Modus, keine globale Achse mehr

    async def forward(self, payload: EgressPayload, scope: Scope | None) -> Sanitized:
        """Roh → egress-fertig. Der Modus entscheidet, OB er den Verifier (§5) komponiert.
        passthrough: Identität, kein Verifier. strict/full: transformiert + Verifier fail-closed."""

    async def reverse_text(self, text: str, scope: Scope) -> str:
        """Sanitisiert → roh. NUR reversible=True. Sonst NotImplementedError."""

    def stream_reverser(self, scope: Scope) -> StreamReverser:
        """Holdback-Puffer: ein Pseudonym kann über zwei Chunks reichen. NUR reversible=True."""

    async def reverse_obj(self, obj: dict, scope: Scope) -> dict:
        """Tool-Call-Argumente zurückmappen. NUR reversible=True."""
```

**Verifier/Detektoren/Audit sind Bausteine (§3.1), kein Zwang.** Ob ein Modus den Verifier
komponiert, ist Sache des Modus. Die *eingebauten* Sanitisierungs-Modi komponieren ihn
**fail-closed**, damit sie out-of-the-box vertrauenswürdig sind; `passthrough` tut es nicht; ein
Fremd-Modus ist frei. Guard-Orchestrierung (Profil-Gate zuerst, Audit-Aufruf) und Dispatch
bleiben **außerhalb** des Modus und für alle Modi gleich.

**Eingebaute Modi (initiale Menge, erweiterbar):**
- **`strict`** — `reversible=False`, **Auslieferungs-Default (§4.3)**. **Auto-redigierend:**
  `forward()` fährt selbst die Detektor-Engine (§5.1) über den *Rohtext*, ersetzt jeden Treffer
  durch einen typisierten Platzhalter (`[IP]`, `[EMAIL]`, `[SECRET]`, …) und komponiert danach
  den Verifier **fail-closed** — bleibt ein roher Identifier stehen, wird **blockiert** (kein
  Durchlass mit Rest-Leck). Anders als `generalizing` verlangt `strict` *keinen* vom Konsumenten
  vor-generalisierten Text; er sanitisiert eigenständig. Irreversibel (kein Mapping). Der Modus,
  der die früheren „Invarianten"-Eigenschaften trägt.
- **`passthrough`** — `reversible=False`, kein Verifier, keine Transformation (§2.1). Opt-in.
- **`generalizing`** — `reversible=False`. `forward()` verifiziert nur, dass der vom Konsumenten
  *bereits generalisierte* Text egress-tauglich ist (die semantische Generalisierung macht der
  Konsument, §1.1). Herkunft: Tempers `guard.py` (ruft nur `verify_no_identifiers`).
- **`pseudonymizing`** — `reversible=True`. Volle Implementierung aller vier Methoden; trägt den
  Mapping-Lebenszyklus (§8). Herkunft: PrismClaws `anon` (`forward_messages`, `reverse_text`,
  `stream_reverser` mit Holdback, `reverse_obj`).
- *(erweiterbar)* ein regex-`basic` / regex+NER-`full`-Abstufung sowie **format-preserving**
  (Beträge/IBANs strukturerhaltend) sind je ein weiterer registrierter Modus — **ohne** Guard,
  Profil-Gate oder Audit anzufassen. *(post-v1)*

Modus-Namen benennen **Absicht/Zusage**, nicht die Engine: nicht `pii_regex`/`pii_ner` (das
verdrahtet die Implementierung in den Vertrag und bricht beim Engine-Tausch, §5.1/§7.4), sondern
Abstufungen wie `basic`/`full`/`strict`. Regex-vs-NER bleibt austauschbare Engine dahinter.

---

## 4. Profil-Schema (der Schalter, maschinenlesbar)

Jeder Konsument deklariert **ein Profil**. Das Profil ist die vollständige, auditierbare Form
seiner Egress-Erlaubnis.

```toml
[profile."temper"]
mode                = "generalizing"          # Modus-Name aus der Registry (§3)
egress_enabled      = true                    # false = souverän/air-gapped: NICHTS raus (§4.2)
allowed_purposes    = ["external_escalation", "promotion_upload"]
provider_allowlist  = ["claude", "gemini"]    # welche Provider dieses Profil überhaupt darf (§4.1)
detector_profile    = "infra"                 # welches Detektor-Set der Verifier lädt (§5.1)

[profile."aider-code"]
mode                = "pseudonymizing"        # reversibler Modus — explizites Opt-in (§2)
egress_enabled      = true
allowed_purposes    = ["code_completion"]
provider_allowlist  = ["claude"]
detector_profile    = "code"
  [profile."aider-code".reversible]
  scope             = "session"               # Mapping-Scope (§8)
  ttl_seconds       = 3600
  storage           = "memory"                # memory | persistent(post-v1)

[profile."crate"]
mode                = "strict"                # auto-redigiert das rohe Operator-Thema (Rev. 9)
egress_enabled      = true
allowed_purposes    = ["playlist_curation"]
provider_allowlist  = ["claude", "gemini", "openai"]
detector_profile    = "media"
dictionary_terms    = ["Richard", "Musterstraße 12"]  # Namen/Adressen, die Regex nicht fängt (§5.1, Rev. 10)

[profile."bank-tool"]                         # strengstes Profil (§4.4)
mode                = "strict"                 # kein consumer-generalisierter Text: harter Modus
egress_enabled      = true
allowed_purposes    = ["strategy_reasoning"]
provider_allowlist  = ["claude-zdr"]          # nur Zero-Retention / lokal
detector_profile    = "financial"
```

### 4.1 Allowlists pro Profil (Provider und Modus)
Weil Modell-Adapter generisch sind (§7.3), unterscheidet sich die *Provider*-Erlaubnis pro Profil
— Provider divergieren in Retention/Training. Bank-Profil ⇒ nur ZDR/lokal; Crate darf großzügiger.

**Modus-Allowlist (`allowed_modes`, optional, Rev. 9).** Analog begrenzt ein Profil, welche
Registry-Modi (§3) ein Request wählen darf. **Default: alle registrierten Modi erlaubt** — maximale
Freiheit für Konsumenten/Experimente (Rev. 9). Ein sicherheitsbewusster Betreiber sperrt schwache
Modi gezielt: `allowed_modes = ["strict"]` verbietet z. B. `passthrough` für dieses Profil, auch
wenn ein Request ihn anfragt (⇒ 403, fail-closed). Wichtig: der *laufende Default* bleibt in jedem
Fall `strict` (§4.3) — die Allowlist erweitert/beschränkt nur die *wählbaren* Modi, sie ändert nie,
was ohne explizite Wahl passiert.

### 4.2 `egress_enabled = false` = souveränes Profil
Maschinenlesbare Form von Prinzip 16 (Tempers `policy.py`). Guard lässt **nichts** durch,
egal welcher Modus — der Riegel greift *vor* der Modus-Auswahl.

### 4.3 Default-Deny & sicherer Default-Modus
Ein Konsument **ohne** Profil bekommt **nichts** raus. Kein Vererben fremder Profile.
Neue Projekte erben nie versehentlich Crates lockere Policy.

**Sicherer Default-Modus (Rev. 9):** Lässt ein Profil das `mode`-Feld weg, gilt **`strict`** —
nicht `passthrough`. Die schwachen Modi (`passthrough`, fail-open) sind **nur** wirksam, wenn das
Profil sie ausdrücklich nennt bzw. der Request sie setzt (§7.2). *Safe by default:* ein Vertippen
oder ein kopiertes Skelett-Profil öffnet nie versehentlich die Boundary; „offen" muss dastehen.

### 4.4 Mandanten-Isolation
Geteilter Code, aber getrennte Policies, Audit-Streams, Credentials und Budgets **pro Profil**.
Ein Bug/eine schlampige Regel in `crate` darf niemals `temper`- oder `bank-tool`-Daten mitreißen.

---

## 5. Der Verifier (universeller, deterministischer Backstop)

Herkunft: Tempers `verifier.py`. **Ein wiederverwendbarer Baustein (Rev. 9), kein globaler
Zwang mehr.** Verhalten unverändert, wo er läuft: fail-closed — findet er *irgendeinen* rohen
Identifier → **blockiert** (`clean=False`). Lieber false-positive als ein Leck. Die *eingebauten*
Sanitisierungs-Modi (`strict`, `full`, `pseudonymizing`) **komponieren ihn fail-closed** als
harten Boden *unter* der probabilistischen Transformation — er bleibt dort der Riegel, den
„reversibel" nie lockert. `passthrough` komponiert ihn *nicht*; ein Fremd-Modus ist frei. Ein
Modus, der ihn weglässt, gibt damit dessen Zusage bewusst auf — das ist die Konsumenten-
Entscheidung aus Rev. 9, nicht Sluices Standard (der ist `strict`, §4.3).

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

#### 5.1.1 Profil-**Wörterbuch** `dictionary_terms` (Rev. 10)

Die Regex-Muster erkennen strukturierte Identifier (IP, E-Mail, Host, Pfad), **nicht** aber
freie **Namen und Adressen** („Richard", „Musterstraße 12") — die haben keine generische Form.
Genau diese Deckung leistete das `dictionary` in PrismClaws/Crates anon. Sluice zieht sie als
**profil-deklarierte Term-Liste** ein:

```toml
dictionary_terms = ["Richard", "Musterstraße 12", "Acme GmbH"]
```

- **Matching (Engine):** jeder Term wird **literal** (regex-escaped), **wortgrenzen-gebunden**
  und **case-insensitiv** gematcht — „Richardson" ist nicht „Richard".
- **Wirkung:** `strict` redigiert Treffer zu `[NAME]`; der Verifier führt sie als Befund und
  blockt **fail-closed in jedem Modus** (auch `generalizing`/`pseudonymizing` — der Boden gilt
  für alle, §5). Ein Modus muss die Terme nicht kennen; der Verifier fängt sie darunter.
- **Mechanismus/Domäne (§1.1):** die *Liste* ist Domäne (der Konsument weiß, welche Namen in
  seinem Haushalt/Kontext vorkommen) → im Profil. Das *Matching* ist geteilte Engine → Sluice.
- **Grenze:** literal, nicht semantisch — Flexionen/Tippfehler/unbekannte Namen deckt erst NER
  (§5.2, post-v1). Für einen bekannten, überschaubaren Term-Satz (Haushaltsnamen) ist das
  deterministische Wörterbuch die richtige, prüfbare Antwort.

### 5.2 Genericity-Check (Re-Identifikation durch Kombination) *(post-v1)*
Reifeversion: nicht nur „enthält Identifier?", sondern „generisch genug, um aus vielen
Umgebungen zu stammen?". Justierbar pro Profil (bank-tool strenger als temper).

---

## 6. Audit (`egress_log`, append-only)

*Ein* Log, das der Guard beim Durchlass schreibt — released *und* blocked. Löst Tempers
heutiges `TODO(post-v1)` in `guard.py::_log_egress` in Sluice ein.

**Detailgrad konfiguriert der Betreiber (Rev. 9), nicht der Modus** — global per
`SLUICE_AUDIT_LEVEL`:
- **`full`** — voller Eintrag inkl. reviewbarem Vorher/Nachher (`before`/`after`). Die Fläche
  fürs Admin-Gate und der stärkste operative Nachweis.
- **`metadata`** (Default) — Eintrag *ohne* `before`/`after`: `timestamp, profile, purpose, mode,
  released(bool), reason, provider_target, verifier_findings[]`. Man sieht *dass* etwas durchging
  und in welchem Modus, ohne die Nutzdaten zu persistieren.
- **`off`** — kein Log. Bewusste Betreiber-Entscheidung; Sluice sagt dann nichts über Durchläufe
  zu.

Wo geschrieben wird, ist der Sink **append-only** (Prinzip 13). Der Detailgrad ist eine
*Betriebs*-Einstellung und **verändert den Sanitisierungs-Modus nicht** — `strict` bleibt
`strict`, auch wenn der Betreiber `off` fährt.

---

## 7. Der Vertrag: Konsument ↔ Sluice

Der Schalter wählt nicht nur einen Algorithmus, sondern eine **Integrationsform**. Beide
Formen münden in denselben geguardeten Kern (Profil-Gate → Modus → Verifier → Audit).

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

`mode` (optional): ein **Modus-Name aus der Registry** (§3) — `strict` | `passthrough` |
`pseudonymizing` | … — überschreibt den Profil-Modus für diesen Request (Rev. 9; die alten Werte
`"irreversible"`/`"reversible"` bleiben als Alias auf `strict`/`pseudonymizing` gültig). Fehlt er,
gilt der Profil-Modus, sonst `strict` (§4.3). Der gewählte Modus wird gegen die optionale
`allowed_modes`-Allowlist des Profils geprüft (§4.1); eine nicht erlaubte Wahl ⇒ fail-closed
(403). `provider` (optional): default ist der erste Eintrag der Provider-Allowlist; jede Wahl
wird gegen sie geprüft (§4.1).

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
- **Reihenfolge zwingend:** Der Adapter wird ausschließlich vom Dispatch aufgerufen, *nachdem*
  der gewählte Modus gelaufen ist und der Guard released hat — der Adapter ist die *letzte*
  Schicht der Kette, **nie ein Bypass** daran vorbei (§2). Bei sanitisierenden Modi heißt das
  „nie mit unverifiziertem Text"; bei `passthrough` reicht der Modus den Text bewusst
  unverändert durch — die Reihenfolge (Gate → Modus → Dispatch) gilt trotzdem.
- **API-Keys:** per Env-Variable (`SLUICE_ANTHROPIC_API_KEY`, `SLUICE_OPENAI_API_KEY`,
  `SLUICE_GEMINI_API_KEY`, `SLUICE_MISTRAL_API_KEY`) — seit Revision 7 **nur in der Env
  des jeweiligen Gateway-Prozesses**, nie beim Kern. Nie im Profil-TOML, nie im Audit-Log.
  Fehlender Key ⇒ fail-closed (Fehler, kein stiller Fallback auf anderen Provider).
- **Timeouts:** endlicher Connect-Timeout, **kein Read-Timeout** (agentische Turns streamen
  lange) — wie im übrigen Code.
- **Antwortpfad:** bei `pseudonymizing` läuft die Provider-Antwort durch `reverse_text` /
  `stream_reverser` / `reverse_obj` derselben Modus-Instanz (Scope-Konsistenz, §8).
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
- **User-Isolation (Revision 8):** jede Gateway-Instanz läuft unter ihrem **eigenen
  System-User** `sluice-gw-<provider>` (Template-Unit: `User=sluice-gw-%i`), der Kern
  unter `sluice`. Damit ist die Key-Isolation auch auf OS-Ebene durchgesetzt: kein
  Gateway kann Prozesse, Dateien oder Env eines anderen Gateways (oder des Kerns)
  lesen, und beim Umzug eines Gateways auf einen eigenen Server wandert genau dieser
  eine User mit — das Betriebsmodell ändert sich beim Split nicht.
- **Provider-Lock für Kern-Instanzen (Revision 5, optional):** `SLUICE_PROVIDER` beschränkt
  eine Kern-Instanz auf genau einen Provider (fremde Provider ⇒ 403, fail-closed; Allowlist
  §4.1 gilt unverändert) — zusätzliche Schranke, kein Ersatz für die Gateway-Services.

### 7.4 Versionierte Schnittstelle (ab v1 Pflicht)
Sluice ist ab jetzt eine **Abhängigkeit mit Vertrag** — ein Breaking Change trifft alle
Konsumenten gleichzeitig. `Accept: application/vnd.sluice.v1+json` bzw. `/v1/…`-Pfad-Präfix
und eine Kompatibilitätszusage von Beginn an, sonst wird jedes Update zur Drei-Repo-Migration.

---

## 8. Mapping-Lebenszyklus (nur pseudonymizing-Modus)

Der Zustand, den der reversible Modus einführt und der generalisierende nie hat. Ehrlicher
Zusatzaufwand — der Schalter macht die *Auswahl* billig, nicht den Modus selbst.

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
- `sluice/guard.py` ← Tempers `egress/guard.py` (Orchestrierung: Gate → Modus → Verifier → Audit).
- `sluice/policy.py` ← Tempers `egress/policy.py`, erweitert um das Profil-Schema (§4).
- `sluice/verifier.py` ← Tempers `egress/verifier.py` **unverändert im Kern**; Muster in
  Detektor-Profile (§5.1) ausgelagert.
- `sluice/audit.py` ← `egress_log`, append-only (§6) — löst Tempers TODO ein.

**Phase 2 — Modi.**
- `sluice/modes/generalizing.py` ← forward-only (aus Tempers Datenfluss).
- `sluice/modes/pseudonymizing.py` ← PrismClaws `prismclaw.anon`: `forward_messages`,
  `reverse_text`, `stream_reverser` (Holdback), `reverse_obj`, `scope`/TTL (§8).
- `Mode`-Interface (§3); Auswahl über Profil.

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
- **Format-preserving-Modus** fürs Bank-Tool als weiterer registrierter Modus (§3) — *(post-v1)*.
- **`egress_log`-Persistenz** über reines Logging hinaus — *(post-v1)*.
- **mTLS Konsument ↔ Sluice** als Härtung (heute VLAN-firewalled) — *(post-v1)*.
- **Bank-Tool-Lesezugriff** (FinTS/HBCI/Aggregator) ist eine *separate* Sicherheitsfläche,
  **nicht** Teil von Sluice — nur der Vollständigkeit halber genannt.
- **Open-Source-Split (Rev. 9)** — *(offen)*, eigene Phase: generischer Kern (Modus-Registry,
  Verifier-Engine, Audit, Server, Gateway) öffentlich; konkrete Profile, Detektor-Muster echter
  Konsumenten und Deploy-Configs mit Homelab-Details (IPs, `sluice.env`, Konsumentennamen) als
  **privates Overlay**. Vor jeder Veröffentlichung: Scrub gegen private Details. Es ist dieselbe
  Mechanismus/Domäne-Grenze wie §1.1, jetzt als Repo-Grenze.
- **Öffentliches Modus-Plugin-API (Rev. 9)** — sobald Dritte eigene Modi registrieren, wird das
  `Mode`-Interface (§3) ein **öffentlicher Vertrag** und fällt unter die Versionszusage §7.4.
  Registrierungs-Mechanik (Entry-Points vs. explizite Registry) — *(offen)*.
- **Lizenzwahl** für den Open-Source-Kern — *(offen)*.

---

## Anhang A — Landschaft nach der Konsolidierung

| Projekt | Rolle ggü. Sluice | Modus (§3) | Integrationsform |
|---|---|---|---|
| **Sluice** | *ist* die Boundary | — | — |
| **PrismClaw** | Konsument (verliert eingebackene `anon`; Gateway-Rolle entfällt mit Revision 2, §7.3) | — / pseudonymizing | Proxy (§7.2) |
| **Temper** | Konsument | generalizing | Guard-Call (§7.1) |
| **Crate** | künftiger Konsument | generalizing | Guard-Call (§7.1) |
| **Aider/Code** | Konsument | **pseudonymizing** (Opt-in) | Proxy (§7.2) |
| **Bank-Tool** | künftiger Konsument | generalizing (financial) | Guard-Call (§7.1) |
