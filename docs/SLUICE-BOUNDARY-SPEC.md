# Sluice — Boundary- & Contract-Spec (v1)

> **Stand:** 2026-09-03. **Revision 15: die geprüfte Fläche** — drei zusammengehörige
> Korrekturen an *einer* Frage: welchen Text sieht der Verifier überhaupt?
> **(a) §5.5 (neu) — jede Content-Fläche wird geprüft, nicht nur `content:str`.** Block-Listen,
> Tool-Result-Inhalte und Tool-Argumente liefen bis hierher **unredigiert und unverifiziert**
> durch, während das Audit `released=true` meldete. Kein Ausfall, der blockiert, sondern ein
> Durchlass, den niemand sah — dieselbe Klasse wie die stille Kürzung (§5.3) und ab jetzt
> genauso behandelt: was sich nicht als Textfläche aufzählen lässt (Bilder, unbekannte
> Blocktypen), **blockiert** unter jedem Modus mit Verifier.
> **(b) Tool-Specs sind geprüfte Fläche** (§7.2). Sie stehen jetzt im **Guard-Payload**
> (`EgressPayload.tools`) statt als Dispatch-Parameter daneben — es gibt damit keinen Weg,
> Tools zu senden, ohne dass der Guard sie sieht; die Chokepoint-Eigenschaft ist strukturell,
> nicht per Konvention (§1). Verifiziert werden sie als `readonly` (geprüft, nie
> umgeschrieben). **Damit ist Tool-Calling unter *jedem* Modus möglich** — die Rev.-14-Grenze
> „nur unter `passthrough`" ist aufgehoben, und der Fall, für den Sluice gebaut ist
> (agentische Konsumenten unter `strict`), ist kein Passthrough-Fall mehr.
> **(c) Beide Dialekte am Endpoint** (§7.2). `/v1/chat/completions` nimmt die neutrale *und*
> die OpenAI-Tool-Form an und antwortet in dem Dialekt, in dem die Anfrage kam. Die
> Übersetzung sitzt allein in `sluice/dialect.py` und läuft **vor** dem Guard, damit
> Tool-Argumente als aufgelöste Werte geprüft und redigiert werden statt als undurchsichtiger
> JSON-String. Ohne diese Schicht ist Sluice für genau die Konsumenten unerreichbar, für die
> es gebaut ist — wer den Endpoint als „OpenAI-kompatiblen Provider" einträgt, spricht diesen
> Dialekt. **Fail-closed bleibt, wo Sluice eine Zusage nicht einlösen kann:** nicht-tool-fähiger
> Provider, Streaming mit Tools (§6.5), erzwungenes `tool_choice`.
> **(d) Tool-Calling in *allen* v1-Adaptern** (§7.3) — `anthropic`, `openai`, `mistral`,
> `gemini`. Rev. 14 hatte nur `anthropic`; damit wäre jeder agentische Konsument faktisch
> Claude-only gewesen, und die Provider-Allowlist (§4.1) hätte für Tool-Turns nur einen
> Eintrag zur Wahl gehabt. Für die OpenAI-Familie ist „nativ" derselbe Dialekt wie am
> Endpoint — dieselben Funktionen, keine zweite Abbildung, die auseinanderlaufen kann.
> `gemini` braucht eine echte Übersetzung samt Auflösung `tool_call_id → Tool-Name`; ist
> sie nicht möglich, wird **blockiert** statt geraten.
>
> **Bewusst noch offen:** Streaming von `tool_calls` (§6.5 — der Loop läuft
> nicht-streamend) und ein erzwingendes `tool_choice`. Beides wird abgewiesen, nicht
> stillschweigend ignoriert.
>
> **Revision 14:** **Tool-Calling wird erstklassig im Proxy- und
> Adapter-Vertrag** (§7.2/§7.3). Der §7.2-Endpoint nimmt optional `tools` (neutrale Specs
> `{name, description, input_schema}`) entgegen und gibt `tool_calls` (neutral,
> `{id, name, arguments}`) + `finish_reason` zurück; Adapter-Interface (§7.3) und interner
> Kern→Gateway-Vertrag `/v1/complete` (§7.4) tragen beides mit. **Additiv/rückwärts­kompatibel:**
> ohne `tools` ist das Verhalten byte-gleich zu Rev. 13. ~~**Fail-closed-Grenze:** Tools sind nur
> unter einem Modus *ohne* Verifier (`passthrough`, `enforce_verifier=false`) freigegeben~~
> — **durch Rev. 15 (b) ersetzt:** die Specs sind jetzt selbst geprüfte Fläche, Tool-Calling
> läuft unter jedem Modus. Bestehen bleibt: ein nicht-tool-fähiger Provider
> (`TOOL_CAPABLE_PROVIDERS`) mit `tools` wird **blockiert**, nie still ohne Tools
> weitergereicht — ein Chokepoint, der Tools klaglos verschluckt, ist keiner. Der Konsument
> fährt den agentischen Loop weiter (führt Tools lokal aus, ruft pro Runde erneut) — Keys und
> Provider-Call bleiben in Sluice. Erster Durchstich: Adapter `anthropic`, nicht-streamend
> (der Loop läuft ohnehin nicht-streamend, §6.5); Streaming von `tool_calls` und weitere Adapter
> folgen additiv. **Revision 13:** `detector_profile` nimmt **einen Namen oder eine
> Liste** (§5.1). Grund: ein Profil braucht regelmäßig *beides* — die Muster seiner Domäne
> und die deutschen PII-Muster. `media` allein kennt keine IBAN, `pii_de` allein keine
> NAS-Pfade; wer sich entscheiden muss, tauscht Schutz in der eigenen Domäne gegen Schutz
> in einer fremden. Mehrere Namen werden zur **Vereinigungsmenge** zusammengelegt und unter
> einem kanonischen Namen (`media+pii_de`) geführt — nach außen bleibt es *ein* Name, damit
> Verifier, Span-Erkennung und Anonymisierungs-Identität (§5.4) unverändert weiterarbeiten.
> Der Name ist **sortiert**, damit eine Umordnung im Profil den Digest nicht bewegt.
> **Rückwärtskompatibel:** ein einzelner String verhält sich wie bisher, inklusive Digest.
> **Eine Verschärfung:** ein **unbekannter** Profilname ist ab Rev. 13 ein *Ladefehler*.
> Bis Rev. 12 lud ein Tippfehler klaglos und der Verifier blockte erst zur Laufzeit —
> fail-closed zwar, aber als Fehlerbild irreführend: man sucht dann einen Defekt statt
> eines Zeichendrehers.
>
> **Revision 12:** **zweistufige PII-Erkennung** — die Modi
> `pii_regex` und `pii_regex`+NER `pii_ner` (§3, §5.3), der eigenständige **NER-Dienst**
> (§7.5) und die **Anonymisierungs-Identität** (§5.4). `pii_ner` ist **additiv, nicht
> alternativ**: beide Stufen laufen, das Ergebnis ist die **Vereinigungsmenge** der Spans;
> bei Überlappung hat die **Regex-Erkennung Vorrang**, weil nur sie den per Prüfsumme
> *validierten* Typ kennt. Das Modell ersetzt die Regex-Stufe nie — es übernimmt
> ausschließlich, was Regex prinzipiell nicht kann (Namen, Organisationen, Freitext-
> Adressen, Ortsangaben). **Fail-closed auf der Verfügbarkeits-Achse:** ist der NER-Dienst
> weg oder reißt das Timeout, wird **blockiert** (503 `sluice_mode_unavailable`) — kein
> stiller Rückfall auf `pii_regex`, keine Degradation. Der Guard greift dabei generisch am
> Fehlertyp (`ModeUnavailableError`), nicht am Modus-Namen. **Der Schwellwert wird auf
> Recall optimiert, nicht auf F1** (§5.4) und ist ein *versionierter Konfigurationswert*,
> keine Code-Konstante. Additiv und rückwärtskompatibel: bestehende Profile und Modi
> bleiben unverändert.
>
> **Namensregel aus Rev. 9 aufgehoben (bewusste Vertragsentscheidung).** Rev. 9 verbot
> engine-benennende Modus-Namen (`pii_regex`/`pii_ner`) zugunsten von Abstufungen
> (`basic`/`full`). Rev. 12 hebt das für diese zwei Modi auf: die Namen benennen die
> Engine, und das ist hier gewollt — die *Zweistufigkeit selbst* ist die Zusage an den
> Konsumenten, nicht bloß ein Implementierungsweg dahin. Wer `pii_ner` wählt, wählt
> ausdrücklich „Regex **und** Modell", mit allem, was daran hängt: einer externen
> Abhängigkeit, einem Recall-Schwellwert, einer Modellidentität im Audit. Ein neutraler
> Name wie `full` würde genau das verbergen. Der eingetauschte Preis ist real und wird
> hier festgehalten: ein Engine-Tausch *innerhalb* der NER-Stufe (anderes Modell, andere
> Runtime) ist weiterhin frei — der Modell-Name steht in der Konfiguration, nicht im
> Modus-Namen —, aber ein Wechsel der *Erkennungsart* wäre ein neuer Modus, kein stiller
> Austausch hinter demselben Namen. Für künftige Modi gilt die Rev.-9-Regel weiter.
> **Revision 11:** `allowed_modes` wird **fail-closed für fail-open-Modi**
> (§4.1/§4.3). Die leere Allowlist erlaubt weiterhin alle **verifizierenden** Modi
> (`strict`/`generalizing`/`pseudonymizing`) — aber ein **Modus ohne Verifier** (`enforce_verifier=false`,
> heute nur `passthrough`, §2.1) ist **nur** wirksam, wenn das Profil ihn **ausdrücklich** in
> `allowed_modes` nennt. Eine leere/fehlende Allowlist **sperrt** ihn jetzt (⇒ 403), auch wenn ein
> Request ihn wählt. Damit deckt sich das Verhalten mit der Rev.-9-Zusage „*fail-open-Modi sind
> explizites Opt-in*" (§2): Vergessen der Allowlist heißt jetzt **zu**, nicht **offen** — der Riegel
> ist auf der Modus-Achse fail-closed. Rein additiv für verifizierende Modi (unverändert frei);
> Bruch nur für Profile, die `passthrough` bislang *ohne* Nennung in `allowed_modes` nutzten — die
> müssen ihn nun listen. Die Regel greift generisch über `enforce_verifier` (§3), nicht am Namen
> `passthrough` — jeder künftige verifierlose Dritt-Modus erbt dieselbe Opt-in-Pflicht.
> **Revision 10:** Profil-**Wörterbuch** (`dictionary_terms`, §4/§5.1) —
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
- **`pii_regex`** *(Rev. 12)* — `reversible=False`. Die **Regex-Stufe allein**: strukturierte
  Identifikatoren über das Detektor-Profil (`pii_de`), span-basiert redigiert. Wo ein
  Prüfziffernverfahren greift (IBAN Mod-97, Steuer-ID, SVNR, KVNR, Luhn), ist die Erkennung
  präzise — eine bestandene Prüfsumme *ist* die Typbestätigung. Verifier fail-closed (§5.3).
- **`pii_ner`** *(Rev. 12)* — `reversible=False`. **`pii_regex` plus Modellerkennung**, additiv:
  beide Stufen laufen, das Ergebnis ist die **Vereinigungsmenge**. Implementiert als
  *Unterklasse* von `pii_regex`, damit die Regex-Stufe strukturell nicht wegfallen kann.
  Braucht den NER-Dienst (§7.5); ohne ihn wird **blockiert**, nie degradiert (§5.3).
- *(erweiterbar)* **format-preserving** (Beträge/IBANs strukturerhaltend) ist je ein weiterer
  registrierter Modus — **ohne** Guard, Profil-Gate oder Audit anzufassen. *(post-v1)*

Modus-Namen benennen grundsätzlich **Absicht/Zusage**, nicht die Engine — ein Engine-Name im
Vertrag bricht beim Engine-Tausch (§5.1/§7.4). Für neue Modi gilt das weiter: Abstufungen wie
`basic`/`full`/`strict` statt Verfahrensnamen.

**Ausnahme `pii_regex`/`pii_ner` (Rev. 12, bewusst).** Hier *ist* die Zweistufigkeit die Zusage,
nicht bloß der Weg dorthin: wer `pii_ner` wählt, wählt ausdrücklich „Regex **und** Modell" — und
damit eine externe Abhängigkeit, einen Recall-Schwellwert und eine Modellidentität im Audit. Ein
neutraler Name wie `full` würde genau diese Konsequenzen verbergen. Der Preis ist real: ein
Wechsel der *Erkennungsart* wäre ein neuer Modus statt eines stillen Austauschs hinter demselben
Namen. Frei bleibt der Tausch *innerhalb* der NER-Stufe — welches Modell, welche Runtime, welche
Präzision steht in der Konfiguration (§5.4), nicht im Modus-Namen.

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

[profile."prismclaw-ner"]
mode                = "pii_ner"               # Regex ∪ NER (Rev. 12, §5.3)
egress_enabled      = true
allowed_purposes    = ["external_escalation"]
provider_allowlist  = ["claude"]
detector_profile    = "pii_de"                # deutsche PII mit Prüfsummen (§5.1)
  [profile."prismclaw-ner".ner]
  url               = "http://127.0.0.1:17900"  # fehlt sie ⇒ fail-closed, nie Bypass (§5.3)
  threshold         = 0.30                    # RECALL-optimiert, nicht F1 (§5.4)
  labels            = ["person", "organization", "address", "location"]
  timeout_seconds   = 5.0                     # Timeout zählt als Ausfall ⇒ blockiert
  model_repo        = "urchade/gliner_multi_pii-v1"
  model_revision    = "<commit-hash>"         # ohne ihn ist die Identität wertlos (§5.4)
  model_precision   = "fp32"

[profile."crate"]
mode                = "strict"                # auto-redigiert das rohe Operator-Thema (Rev. 9)
egress_enabled      = true
allowed_purposes    = ["playlist_curation"]
provider_allowlist  = ["claude", "gemini", "openai"]
detector_profile    = ["media", "pii_de"]     # Vereinigungsmenge: NAS-Pfade UND deutsche PII (§5.1, Rev. 13)
dictionary_terms    = ["Mustermann", "Musterstraße 12"]  # Namen/Adressen, die Regex nicht fängt (§5.1, Rev. 10)

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

**Modus-Allowlist (`allowed_modes`, optional, Rev. 9; fail-closed für fail-open-Modi seit Rev. 11).**
Analog begrenzt ein Profil, welche Registry-Modi (§3) ein Request wählen darf. Die Semantik der
**leeren** (oder fehlenden) Allowlist ist seit Rev. 11 **abgestuft nach `enforce_verifier`** (§3):

- **Verifizierende Modi** (`enforce_verifier=true` — `strict`, `generalizing`, `pseudonymizing`):
  eine leere Allowlist erlaubt sie **alle** — maximale Freiheit für Konsumenten/Experimente. Diese
  Modi komponieren den Verifier fail-closed (§5), können also nicht versehentlich roh leaken.
- **Fail-open-Modi** (`enforce_verifier=false` — heute nur `passthrough`, §2.1): eine leere Allowlist
  **sperrt** sie (⇒ 403, fail-closed). Sie sind **nur** wirksam, wenn das Profil sie **ausdrücklich**
  in `allowed_modes` listet — auch wenn ein Request sie wählt. *Vergessen = zu.*

Ein sicherheitsbewusster Betreiber muss also nichts *hinzufügen*, um `passthrough` zu sperren — es
ist per Default gesperrt; er *nennt* es explizit (`allowed_modes = ["strict", "passthrough"]`), um es
freizugeben. Umgekehrt sperrt eine nicht-leere Allowlist wie bisher **jeden** nicht gelisteten Modus,
egal ob verifizierend. Wichtig: der *laufende Default* bleibt in jedem Fall `strict` (§4.3) — die
Allowlist erweitert/beschränkt nur die *wählbaren* Modi, sie ändert nie, was ohne explizite Wahl
passiert. Die Regel greift **generisch über `enforce_verifier`**, nicht am Namen `passthrough`: ein
künftiger verifierloser Dritt-Modus (§3) unterliegt automatisch derselben Opt-in-Pflicht.

### 4.2 `egress_enabled = false` = souveränes Profil
Maschinenlesbare Form von Prinzip 16 (Tempers `policy.py`). Guard lässt **nichts** durch,
egal welcher Modus — der Riegel greift *vor* der Modus-Auswahl.

### 4.3 Default-Deny & sicherer Default-Modus
Ein Konsument **ohne** Profil bekommt **nichts** raus. Kein Vererben fremder Profile.
Neue Projekte erben nie versehentlich Crates lockere Policy.

**Sicherer Default-Modus (Rev. 9):** Lässt ein Profil das `mode`-Feld weg, gilt **`strict`** —
nicht `passthrough`. *Safe by default:* ein Vertippen oder ein kopiertes Skelett-Profil öffnet nie
versehentlich die Boundary; „offen" muss dastehen.

**Fail-open-Modi brauchen Profil-Opt-in (Rev. 11):** Ein verifierloser Modus (`enforce_verifier=false`,
heute `passthrough`) ist **nur** wirksam, wenn ihn die **`allowed_modes`** des Profils ausdrücklich
nennt (§4.1). Ihn per **Request** zu wählen genügt **nicht**, wenn das Profil ihn nicht freigegeben
hat — die Request-Wahl wird gegen dieselbe Allowlist geprüft und fail-closed abgewiesen (⇒ 403).
Damit ist „offen" eine **Betreiber**-Entscheidung im Profil, nicht eine, die ein Konsument allein per
Request treffen kann. (Verifizierende Modi bleiben per Request frei wählbar, §4.1 — sie leaken nicht.)

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
| `pii_de` *(Rev. 12)* | deutsche PII **mit Prüfziffernverfahren**: IBAN (Mod-97), Steuer-ID, SVNR, KVNR, Kreditkarte (Luhn) · E-Mail, IPv4/IPv6, MAC, KFZ-Kennzeichen, Telefon (deutsche Vorwahlstruktur) · Secret/Token. Die Regex-Stufe von `pii_regex`/`pii_ner` (§5.3) |

\* Beim Bank-Tool widersprechen sich Sanitisierung und Lösbarkeit maximal (Beträge *sind* der
Nutzen). Auflösung: Rechnen bleibt **lokal**; nur die abstrahierte Strategiefrage (Kategorien/
Spannen statt Salden) geht raus. Das ist Profil-Arbeit, kein Sluice-Mechanismus. *(Details post-v1)*

#### 5.1.0 Mehrere Profile zusammenlegen (Rev. 13)

Die Sets oben sind nach **Domänen** geschnitten, reale Texte sind es nicht. Eine
Playlist-Anfrage enthält NAS-Pfade *und* womöglich eine Abrechnung; ein Ticket enthält
Kundendaten *und* interne Systemverweise. Bis Rev. 12 zwang `detector_profile` zu einer
Wahl, die man nicht gewinnen kann:

| `media` allein | `pii_de` allein |
|---|---|
| kennt NAS-Pfade, interne Hostnamen, `/home/<user>` | kennt IBAN (Mod-97), Steuer-ID, SVNR, KVNR, Kreditkarte (Luhn), Telefon, KFZ |
| kennt **keine** IBAN | kennt **keinen** NAS-Pfad |

Deshalb nimmt `detector_profile` ab Rev. 13 auch eine **Liste**:

```toml
detector_profile = ["media", "pii_de"]        # Vereinigungsmenge beider Muster-Sets
detector_profile = "infra"                    # weiterhin gültig, unverändertes Verhalten
```

Eigenschaften, die dabei zählen:

- **Vereinigungsmenge, keine Reihenfolge-Semantik.** Es gibt keinen Vorrang zwischen den
  Sets; alle Muster laufen. Dubletten (E-Mail und IP stehen in mehreren Sets) werden
  entfernt — sie wären harmlos, blähten aber die Befundliste im Audit auf.
- **Kanonischer, sortierter Name** (`media+pii_de`). Sortiert, weil die Vereinigung
  ordnungsunabhängig ist: eine bloße Umordnung im Profil darf den Identitäts-Digest nicht
  bewegen — dieselbe Überlegung wie bei den Wörterbuch-Termen (§5.4).
- **Nach außen ein Name.** Das zusammengelegte Set wird unter seinem kanonischen Namen in
  der Registry geführt. Verifier, Span-Erkennung, Modi und Identität arbeiten unverändert
  mit *einem* Namen weiter, und im Audit steht die Zusammensetzung ablesbar da.
- **Unbekannter Name ⇒ Ladefehler**, nicht erst ein Laufzeit-Block (siehe Kopf, Rev. 13).

#### 5.1.1 Profil-**Wörterbuch** `dictionary_terms` (Rev. 10)

Die Regex-Muster erkennen strukturierte Identifier (IP, E-Mail, Host, Pfad), **nicht** aber
freie **Namen und Adressen** („Mustermann", „Musterstraße 12") — die haben keine generische Form.
Genau diese Deckung leistete das `dictionary` in PrismClaws/Crates anon. Sluice zieht sie als
**profil-deklarierte Term-Liste** ein:

```toml
dictionary_terms = ["Mustermann", "Musterstraße 12", "Acme GmbH"]
```

- **Matching (Engine):** jeder Term wird **literal** (regex-escaped), **wortgrenzen-gebunden**
  und **case-insensitiv** gematcht — „Mustermannsen" ist nicht „Mustermann".
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

### 5.3 Zweistufige PII-Erkennung: Regex ∪ NER (Rev. 12)

Die Arbeitsteilung folgt aus einer Asymmetrie, nicht aus Bequemlichkeit:

| Stufe | Zuständig für | Warum genau dort |
|---|---|---|
| **Regex** (`detectors/pii_de.py`) | IBAN (Mod-97), Steuer-ID, SVNR, KVNR, Kreditkarte (Luhn), E-Mail, IPv4/IPv6, MAC, KFZ-Kennzeichen, Telefon (deutsche Vorwahlstruktur) | Feste, prüfbare Form. Eine bestandene Prüfsumme *ist* die Bestätigung des Typs — kein NER-Modell erreicht das. |
| **NER** (`ner/`, §7.5) | Personennamen, Organisationen, Freitext-Adressen, Ortsangaben, kontextabhängige Fälle | Kein festes Format. Genau das, was Regex prinzipiell nicht kann — und was `dictionary_terms` (§5.1.1) nur für *bekannte* Terme literal abdeckt. |

**Vereinigungsmenge, nicht Ersetzung.** `pii_ner` fährt beide Stufen; das Ergebnis ist die
Vereinigung der Spans. Die gemeinsame Koordinate ist der **Zeichen**-Offset (`sluice/spans.py`)
— nie der Token-Offset, denn Sluice redigiert Zeichen, und Token-Grenzen sind Engine-Detail.

**Regex hat Vorrang bei Überlappung.** Ein NER-Span, der einen Regex-Span berührt, fällt ganz
weg. Der Vorrang ist kategorisch, nicht längenabhängig: nur die Regex-Stufe kennt den
*validierten* Typ (`[IBAN]`), das Modell nur eine Label-Vermutung. Ein breiter Modell-Span darf
den typisierten Platzhalter weder überschreiben noch zerschneiden. Innerhalb der NER-Stufe
gewinnt der längere Span (mehr maskiert = mehr Recall).

**Fail-closed auf der Verfügbarkeits-Achse.** Ist der NER-Dienst nicht erreichbar, reißt das
Timeout-Budget oder weicht die gemeldete Modellidentität ab, wird die Anfrage **blockiert**.
Kein stiller Rückfall auf `pii_regex`, keine Degradation — ein Chokepoint, der bei Ausfall
durchlässiger wird, ist keiner. Der Fehler geht als *eigener Typ* an den Aufrufer und ins Audit:

| Lage | HTTP | `error.type` |
|---|---|---|
| Policy-Ablehnung (Profil, Allowlist, Verifier) | 403 | `sluice_blocked` |
| Modus-Ausfall (NER weg, Timeout, Identitätsabweichung) | **503** | **`sluice_mode_unavailable`** |

Die Trennung ist betrieblich nötig: eine Ablehnung ist endgültig, ein Ausfall ist ein Vorfall —
er darf im 403-Rauschen nicht untergehen. Der Guard greift generisch am Fehlertyp
(`ModeUnavailableError`), nicht am Namen `pii_ner`; jeder künftige Modus mit externer
Abhängigkeit erbt das Verhalten. Fehlende Dienst-URL wird genauso behandelt wie ein Ausfall —
dieselbe Härte wie die Gateway-Pflicht (Rev. 7).

**Fail-closed auf der Vollständigkeits-Achse: keine stille Kürzung.** Das Modell hat ein
festes Token-Fenster und kürzt längere Eingaben **still** darauf — es meldet keinen Fehler,
es sieht den hinteren Teil schlicht nie. Das ist gefährlicher als ein Ausfall: ein Ausfall
blockiert, eine Kürzung lässt durch, und das Audit protokolliert `released=true` für einen
Text, der nur zur Hälfte geprüft wurde. Deshalb gilt:

1. Der Dienst **blockiert**, wenn tatsächlich gekürzt wurde (413 `ner_text_truncated` ⇒
   `ModeUnavailableError` ⇒ 503). Die Prüfung greift am tatsächlichen Ereignis, nicht an
   einer geschätzten Längenschranke — sie gilt damit für jedes Modell und jeden Tokenizer.
2. Der Kern **zerlegt** lange Texte vorher in überlappende Stücke (`max_chars_per_chunk`,
   `chunk_overlap_chars` im Profil) und rechnet die Offsets auf den Originaltext zurück,
   sodass Punkt 1 im Normalbetrieb nicht eintritt. Die Überlappung ist nicht optional: ohne
   sie würde jede Entität an einer Schnittstelle in beiden Stücken verfehlt.

Beide Zerlegungsparameter sind **Konfiguration und Teil der Anonymisierungs-Identität**
(§5.4), nicht aus dem Dienst abgeleitet: andere Schnitte bedeuten anderen Kontext je Stück
und damit andere Spans. Der Kern prüft beim ersten Kontakt gegen das vom Dienst gemeldete
`max_tokens` und blockiert, wenn die konfigurierte Stückgröße nicht hineinpasst.

**Kein generatives LLM für die Erkennung.** Nichtdeterministisch, an Span-Grenzen
halluzinationsanfällig, latenzseitig untauglich für den synchronen Egress-Pfad.

### 5.4 Anonymisierungs-Identität & Determinismus (Rev. 12)

**Recall vor Precision.** Der Konfidenz-Schwellwert wird auf **Recall optimiert, nicht auf F1**.
Das weicht bewusst von der üblichen Modellkalibrierung ab, und zwar aus einem Grund, der für
eine Egress-Boundary spezifisch ist: F1 gewichtet einen zusätzlichen False Positive genauso wie
einen übersehenen Span. Diese Gewichtung stimmt hier nicht — Übermaskierung kostet Nutzen, ein
übersehener Span kostet die Zusage. Der Schwellwert ist deshalb ein **versionierter
Konfigurationswert** im Profil (`[profile.X.ner] threshold`), **keine Code-Konstante**. Ein
Profil ohne eigenen Wert erbt einen unkalibrierten Default und wird in der Identität als
`threshold_calibrated: false` ausgewiesen — sichtbar, statt still.

**Determinismus.** Verifizierbare Anonymisierung setzt voraus, dass identische Eingaben
identische Spans liefern. Getragen von vier Maßnahmen:

- **Batchgröße fixiert (immer 1).** Kein dynamisches Batching: sonst ändert sich die
  Reduktionsreihenfolge in Gleitkommaoperationen mit der Auslastung, und Grenzfälle am
  Schwellwert kippen zwischen sonst identischen Läufen. Der Dienst weist abweichende
  Batchgrößen ab (400) — die Ablehnung *ist* die Durchsetzung.
- **Ein Intra-Op-Thread.** Gleicher Grund: die Thread-Anzahl ändert die Reduktionsreihenfolge.
- **Numerische Präzision fixiert und ausgewiesen** (`/v1/info` → Identität). Ein Wechsel
  fp32→fp16 verschiebt Scores und damit Grenzfälle; er darf nicht unbemerkt passieren. Im
  ONNX-Betrieb wird die *deklarierte* Präzision gegen die *geladene* Datei geprüft — über
  den Exportnamen und zusätzlich über die Quantisierungs-Operatoren im Graph, weil der
  Name eine Behauptung ist und der Graph nicht. Widerspruch ⇒ der Dienst startet nicht.
- **Zerlegung fixiert und ausgewiesen** (`max_chars_per_chunk`, `chunk_overlap_chars`).
  Andere Schnitte bedeuten anderen Kontext je Stück und damit andere Spans; ohne sie in
  der Identität wäre „gleicher Digest ⇒ gleiche Spans" für lange Texte unwahr (§5.3).
- **Totale Sortierordnung** der Spans — zwei gleich lange Spans an derselben Stelle wären
  sonst nur zufällig geordnet.

Der **Inhalts-Hash-Cache** macht Wiederholungen bitgleich und senkt die Latenz. Über
Prozessneustarts hinweg trägt er die Zusage **nicht** — das tun die vier Punkte oben. Sein
Schlüssel enthält Schwellwert, Labels und Modellidentität; sonst überlebte ein alter Eintrag
eine Konfigurationsänderung und hebelte genau die Identität aus, die er stabilisieren soll.

**Die Anonymisierungs-Identität** beantwortet *„womit genau wurde dieser Text anonymisiert?"* —
die Voraussetzung dafür, dass „anonymisiert" im Audit mehr ist als eine Behauptung. Sie ist
**im Profil verankert**, analog zum Modus-Schalter selbst (§4): dieselbe Stelle, die entscheidet
*ob* sanitisiert wird, legt fest *wie genau*. Sie umfasst Modus, Detektor-Profil, den Digest des
Profil-Wörterbuchs sowie — bei NER-Modi — **Modell-Repo und Revision-Hash**, geladene Präzision,
Schwellwert, Label-Liste und Batchgröße. Abrufbar über `GET /v1/anonymization-identity?profile=…`.

Die Wörterbuch-Terme gehen nur als **Digest** ein: sie sind personenbezogen (Rev. 10) und dürfen
nie ins Audit, ihre *Änderung* muss aber sichtbar sein. `digest()` ist der vergleichbare
Fingerabdruck — ändert sich Schwellwert oder Modell-Revision, ändert er sich, und ein stiller
Modellwechsel wird sichtbar statt unbemerkt die Zusage zu verschieben. Meldet der Dienst über
`/v1/info` eine **andere** Identität als das Profil verankert, wird fail-closed blockiert: sonst
wäre der protokollierte Wert eine Lüge. Ohne festgenagelte `model_revision` ist die Identität
wertlos — das Repo könnte sich unter derselben Kennung ändern.

**Labels sind Konfiguration.** GLiNER nimmt die Entitätstypen zur Laufzeit als Label-Liste
entgegen; sie steht deshalb im Profil und ist Teil der Identität, nicht im Dienst verdrahtet.
Ein Modelltausch braucht damit keinen Code-Eingriff.

**Modellauswahl und Kalibrierung** sind Betriebsarbeit, nicht Spec-Inhalt: mindestens zwei
Kandidaten gegeneinander, deutsche Sprachabdeckung zwingend, Span-Level-Metriken getrennt nach
Entitätstyp, Kalibrierung auf einem separaten Dev-Split. Verfahren, Kandidaten und der Stand der
offenen Messungen: `docs/NER-SERVICE.md`, Skripte `scripts/probe_ner_hardware.py` und
`scripts/eval_ner.py`.

### 5.5 Die geprüfte Fläche: was der Verifier überhaupt sieht (Rev. 15)

Alle Zusagen der §§2–5.4 hängen an einer Frage, die bis Rev. 14 nirgends beantwortet war:
**welchen Text prüft der Verifier eigentlich?** Der Proxy-Vertrag (§7.2) spricht von
`messages` — aber `content` ist in der Praxis nicht immer ein String. Anthropic und OpenAI
schicken Block-Listen (`[{"type":"text","text":…}]`), ein Tool-Result trägt seinen Inhalt
in `content`, ein Assistant-Turn seine Tool-Argumente in `tool_calls`. Wer nur
`isinstance(content, str)` prüft, **redigiert diese Flächen nicht und legt sie dem Verifier
nie vor** — der Egress geht mit `released=true` raus, obwohl nur ein Teil geprüft wurde.

Das ist dieselbe Klasse wie die stille Kürzung im NER-Pfad (§5.3): **kein Ausfall, der
blockiert, sondern ein Durchlass, den niemand sieht.** Ein Ausfall meldet sich; eine nicht
betrachtete Fläche meldet sich nie, und das Audit behauptet trotzdem, geprüft zu haben.
Deshalb gilt hier dieselbe Regel wie dort.

**Drei Kategorien** (`sluice/content.py`), aufgezählt über **einen** Walker, der zugleich
das Wiedereinsetzen macht — Prüfung und Redaktion können damit strukturell nicht
auseinanderlaufen, so wie `pii_ner` die Regex-Stufe strukturell nicht verlieren kann (§5.3):

| Kategorie | Was | Behandlung |
|---|---|---|
| `texts` | `content:str`, Text-Blöcke (`text`/`input_text`/`output_text`), Tool-Result-Inhalte (auch verschachtelt), **Werte** in Tool-Argumenten | geprüft **und** redigiert |
| `readonly` | **Schlüssel** von Tool-Argument-Objekten, die deklarierten **Tool-Specs** (§7.2) | geprüft, nie umgeschrieben |
| `opaque` | Bilder, Audio, unbekannte Blocktypen, nicht aufzählbare Formen | **blockiert** unter jedem Modus mit Verifier |

**Warum `readonly` und nicht einfach redigieren.** Zwei Schlüssel, die auf denselben
Platzhalter fallen, würden zu einem verschmelzen und ein Feld still verschlucken — eine
stille Kürzung als „Fix" wäre schlimmer als das Loch. Und ein redigierter Tool-**Name**
passt zu keinem deklarierten Tool mehr. Steht in einer solchen Fläche ein Identifier, ist
das ein Fehler des Konsumenten: dann **blockiert** der Verifier, statt still etwas
zurechtzubiegen (fail-closed statt stiller Korrektur).

**Warum `opaque` blockiert.** Der Verifier ist ein *Text*-Riegel. Was er nicht lesen kann,
kann er nicht freigeben; ein Bild einer Meldeadresse ist genau der Egress, den `strict`
zusagt zu verhindern. Die Regel greift generisch über `enforce_verifier` (§3), nicht an
Blocktyp-Namen: `passthrough` lässt solche Flächen bewusst durch (§2.1) — dort ist es die
erklärte Konsumenten-Entscheidung, nicht ein Loch.

**Konsequenz für Adapter:** ein Adapter, der eine Content-Form nicht abbilden kann, **meldet
das** (`ProviderError`), statt die Message zu überspringen. Eine übersprungene Message käme
beim Modell nie an, obwohl sie die Boundary passiert hat und das Audit `released=true`
protokolliert — der Prüfbericht spräche über etwas anderes als das Gesendete.

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

**Tool-Calling (Revision 14):** die gesendeten `tools` (Egress) und die zurückgegebenen
`tool_calls` (die vom Modell angeforderten Aktionen — ihre `arguments` können Identifikatoren
tragen) sind neuer Inhalt: `full` gehört ins reviewbare Vorher/Nachher, `metadata` zählt die
Runde. (Rev. 14 stand hier „Modus ist stets `passthrough`" — seit Rev. 15 kann jeder
Modus Tool-Turns fahren, der Audit-Eintrag führt den tatsächlich gewählten.)

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
gilt der Profil-Modus, sonst `strict` (§4.3). Der gewählte Modus wird gegen die
`allowed_modes`-Allowlist des Profils geprüft (§4.1); eine nicht erlaubte Wahl ⇒ fail-closed
(403). **Seit Rev. 11:** ein per Request gewählter **fail-open-Modus** (`enforce_verifier=false`,
z. B. `passthrough`) wird nur durchgelassen, wenn das Profil ihn **ausdrücklich** in `allowed_modes`
listet — eine leere Allowlist sperrt ihn, die Request-Wahl allein reicht nicht (§4.3). `provider` (optional): default ist der erste Eintrag der Provider-Allowlist; jede Wahl
wird gegen sie geprüft (§4.1).

Sluice-intern:  forward(scope) → verify → Provider-Adapter (§7.3) → stream_reverser(scope) → Tool-Args reverse
→ SSE zurück an den Konsumenten in **echten Werten**.
```

**Die zwei kritischen Failure-Modes (must-pass, aus dem Aider-Dogfooding):**
1. **Streaming-Passthrough** — ein Pseudonym kann über zwei SSE-Chunks reichen ⇒ Holdback-Puffer
   im `stream_reverser` (PrismClaw hat die Referenz).
2. **Tool-Call-Passthrough** — Tool-Argumente müssen vor Ausführung zurückgemappt werden
   (`reverse_obj`), sonst bekommt das lokale Tool Pseudonyme statt echter Werte.

**Tool-Calling im Proxy-Vertrag (Revision 14, erweitert in Revision 15).** Bis Rev. 13 blieb
die Tool-Passthrough-Zusage (oben) unimplementiert — die v1-Adapter waren rein textuell.
Rev. 14 macht sie zum expliziten, **modus-übergreifenden** Bestandteil des §7.2-Vertrags;
Rev. 15 nimmt ihr die Beschränkung auf `passthrough` und öffnet den Endpoint für beide
Dialekte:

```
POST /v1/chat/completions
  Body:   { messages, model, provider?, stream?, mode?, tool_choice?,
            tools?: [ {name, description, input_schema}                    # neutral
                    | {type:"function", function:{name, description, parameters}} ] }  # OpenAI

→ 200 { …, "choices":[{ "message":{ role, content,
                          # im Dialekt der ANFRAGE (Rev. 15):
                          "tool_calls":[{id, name, arguments}]?            # neutral
                                    | [{id, type:"function",
                                        function:{name, arguments:"<json>"}}]? },  # OpenAI
                        "finish_reason": "tool_calls" | "stop" | "length" }] }
```

- **Der Kern ist neutral, der Endpoint spricht beide Dialekte (Rev. 15).** Intern trägt `tools`
  `{name, description, input_schema}` und `tool_calls` `{id, name, arguments}` (`arguments` ist ein
  **Objekt**, kein JSON-String). Am Endpoint wird zusätzlich die OpenAI-Form angenommen
  (`{type:"function", function:{name, description, parameters}}` bzw. `function.arguments` als
  JSON-String) — **die Antwort kommt im Dialekt der Anfrage.** Symmetrie statt Schalter: kein
  zusätzliches Feld, das man vergessen kann, und keine Vermutung über den Aufrufer. Ohne diese
  Schicht wäre Sluice für genau die Konsumenten unerreichbar, für die es gebaut ist — wer den
  Endpoint als „OpenAI-kompatiblen Provider" einträgt, spricht diesen Dialekt.
  Die Übersetzung liegt **allein** in `sluice/dialect.py` und läuft **vor** dem Guard: nur so
  sieht der Verifier die Tool-Argumente als aufgelöste Werte statt als undurchsichtigen
  JSON-String (§5.5). Alles hinter dem Endpoint — Guard, Modi, Verifier, Audit, Adapter —
  kennt nur die neutrale Form; der Adapter (§7.3) übersetzt sie ins native Provider-Schema.
- **`tools` sind Egress-Inhalt, kein Sonderfall.** Sie stehen deshalb **im Guard-Payload**
  (`EgressPayload.tools`), nicht als Parameter daneben: es gibt keinen Weg, Tools zu senden,
  ohne dass der Guard sie sieht. Der Guard verifiziert sie als **`readonly`**-Fläche (§5.5) —
  geprüft, nie umgeschrieben, weil ein redigierter Tool-Name zu keinem deklarierten Tool mehr
  passt und ein Schema mit Platzhaltern im Typ kaputt ist. Steht ein Identifier in einer Spec,
  **blockiert** der Verifier; das ist ein Konsumenten-Fehler, keine Sanitisierungs-Aufgabe.
  **Damit läuft Tool-Calling unter jedem Modus**, nicht nur unter `passthrough`.
- **Der Loop bleibt beim Konsumenten.** Sluice liefert pro Runde die `tool_calls`; der Konsument
  führt die Tools lokal auf **echten** Werten aus, hängt `tool`-Results an und ruft erneut. Keys und
  Provider-Call bleiben in Sluice — die Egress-Grenze wandert nicht zum Konsumenten. Unter
  `pseudonymizing` sieht der Konsument Pseudonyme und mappt sie per `reverse_obj` zurück (oben);
  unter `strict`/`pii_*` sind die Argumente im Folge-Turn redigiert — das ist gewollt und der
  Preis dafür, dass ein Tool-Argument kein Schleichweg an der Boundary vorbei ist.
- **Fail-closed bleibt, wo Sluice eine Zusage nicht einlösen kann** — nie stilles Weglassen:
  ein Provider außerhalb `TOOL_CAPABLE_PROVIDERS`; **Streaming (`stream:true`) mit `tools`**
  (der Loop läuft ohnehin nicht-streamend, §6.5); ein **`tool_choice`, das ein Tool erzwingt**
  (`auto`/`none` sind folgenlos und gehen durch). In allen drei Fällen bekäme der Konsument
  sonst eine Antwort ohne die Aktionen, die er angeboten hat, und merkte es nicht.

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
- **Tool-Calling (Revision 14, alle Adapter ab Revision 15):** `complete(..., tools=None)` —
  der Adapter rendert die neutrale Spec `{name, description, input_schema}` in das native
  Schema seines Providers und gibt native Tool-Calls als neutrale `ToolCall`s
  (`{id, name, arguments}`) in der `ProviderResponse` zurück:

  | Adapter | Specs | Calls | Results | Besonderheit |
  |---|---|---|---|---|
  | `anthropic` | `tools` | `tool_use` | `tool_result`-Blöcke | neutrale Spec **ist** das native Schema, kein Umbau; Results einer Runde in *einer* Message |
  | `openai`/`mistral` | `tools` | `tool_calls` | `role:"tool"` | nativ = der OpenAI-Dialekt ⇒ **dieselbe** Abbildung wie am Endpoint (`sluice/dialect.py`), nicht eine zweite |
  | `gemini` | `functionDeclarations` | `functionCall` | `functionResponse` | echte Übersetzung, zwei Eigenheiten (unten) |

  **Gemini, zwei Eigenheiten.** (1) Es adressiert ein Result über den **Tool-Namen**, die
  neutrale Form über die `tool_call_id`; der Adapter löst den Namen aus den vorangegangenen
  `tool_calls` auf und **blockt fail-closed**, wenn das nicht geht — ein falsch zugeordnetes
  Tool-Result ist schlimmer als ein Fehler. (2) Es vergibt **keine Call-ID**; Sluice erzeugt
  sie deterministisch aus Name und Position, weil der neutrale Vertrag eine braucht. Der
  Konsument schickt genau diese ID zurück, wo sie wieder zum Namen aufgelöst wird — der Kreis
  schließt sich in Sluice, ohne dass Gemini je eine ID sieht.

  Welche Adapter Tools tragen, führt `TOOL_CAPABLE_PROVIDERS`. Die Liste bleibt **explizit**
  und ist bewusst *nicht* `CANONICAL_PROVIDERS`: ein künftiger Adapter ohne Tool-Übersetzung
  würde sonst allein durch seine Registrierung als tool-fähig gelten und Tools still
  verschlucken. Ein Request mit `tools` an einen nicht gelisteten Provider wird fail-closed
  abgewiesen (§7.2), nie ohne Tools weitergereicht — und dieselbe Prüfung steht ein zweites
  Mal im Gateway (§7.4), damit ein fehlkonfigurierter Kern eine klare Absage bekommt.
- **Failover/Health/Tenant-Order:** *(post-v1)* — v1 ruft genau den einen per Profil
  erlaubten und vom Konsumenten gewählten Provider.
- **Eigenständige Gateway-Services (Revision 6):** jedes Provider-Gateway ist ein
  **eigener Service** (`sluice/gateway.py`, systemd-Template `deploy/sluice-gateway@.service`,
  Ports ab **17890**: anthropic 17890, openai 17891, gemini 17892, mistral 17893) mit eigenem
  Lebenszyklus — Kern und Gateways können jederzeit auf **getrennte Server** umziehen; der
  Kern kennt ein Gateway nur über `SLUICE_GATEWAY_<PROVIDER>_URL` und braucht dann selbst
  **keinen Provider-Key** (Key-Isolation: jedes Gateway hält nur seinen eigenen).
  Interner Vertrag Kern → Gateway (versioniert, §7.4): `POST /v1/complete`
  (messages/model/max_tokens[/stream][/**tools** Rev. 14] → `{text, model, provider`
  [`, tool_calls, stop_reason` Rev. 14]`}` bzw. SSE `data: {"delta": …}` + `[DONE]`),
  `GET /v1/health`. `tools`/`tool_calls` sind additiv: fehlen sie, ist der Body
  byte-gleich zu Rev. 13 (ein Gateway ohne Rev.-14-Kenntnis bleibt kompatibel).
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

### 7.5 NER-Dienst (Revision 12)

Ein **eigenständiger Prozess mit bewusst schmaler Schnittstelle**. Er tut genau eins: Text rein,
Spans raus.

```
POST /v1/detect   (= /detect)   {"text": "…", "labels": ["person", …]}
                              → {"spans": [{"start": int, "end": int, "label": str, "score": float}]}
GET  /v1/health   (= /health)   Readiness
GET  /v1/info     (= /info)     {model, revision, precision, labels, backend, score_floor, batch_size, max_tokens}
```

`start`/`end` sind **Zeichen**-Offsets, nicht Token-Offsets. Versionierte Pfade sind der Vertrag
(§7.4); die unversionierten bestehen als Alias.

**Nicht im NER-Dienst:** Pseudonym-Zuordnung, Modus-Schalter, Profilbindung, Maskierungslogik,
Schwellwert-Anwendung. Alles davon bleibt in Sluice. Die Enge ist kein Selbstzweck, sondern die
Bedingung dafür, dass das Modell **austauschbar** bleibt und **zwei Modelle vergleichend**
betrieben werden können, **ohne den Chokepoint zu duplizieren**. Jede Anonymisierungslogik, die
hierher wandert, wäre ein zweiter Riegel neben Sluice.

Die Schwellwert-Anwendung liegt bewusst in Sluice, nicht im Dienst: der Schwellwert ist Teil der
Anonymisierungs-Identität (§5.4). Der Dienst filtert nur grob über `score_floor` vor — und dieser
Wert **muss unter jedem Profil-Schwellwert liegen**, sonst wäre die verankerte Schwelle
wirkungslos. Sluice prüft das fail-closed.

`/v1/info` ist der Grund, warum die Modellidentität überhaupt verankerbar ist: Sluice übernimmt
Modellname, Revision und geladene Präzision daraus ins Profil und blockiert bei Abweichung (§5.4).

**Betrieb** wie die Provider-Gateways (Rev. 6/8): interner Dienst hinter der Boundary, eigener
System-User `sluice-ner`, nie direkt von Konsumenten erreichbar, optionales Shared Secret
`SLUICE_NER_TOKEN`. Port 17900 (Gateways ab 17890). Er sieht **Rohtext** — für ihn gilt dieselbe
Netz-Regel wie für den Kern, nicht die lockere eines Hilfsdienstes.

**Transport-Schranke.** Der NER-Dienst ist die einzige Komponente, die *unsanitisierten* Text
sieht. Dieser Hop trägt damit **mehr** Personenbezug als der spätere Provider-Aufruf — und der
geht über TLS. Läuft der Dienst deshalb auf einem anderen Host, ist eine `http://`-URL nur nach
ausdrücklichem Opt-in (`SLUICE_NER_ALLOW_PLAINTEXT_REMOTE`) erlaubt; Loopback und `https://`
sind frei. Bewusst ein Opt-in und kein Verbot: einen WireGuard-Tunnel unter dem `http://` kann
Sluice nicht sehen und würde einen korrekt abgesicherten Aufbau sonst fälschlich blockieren.
Ein Shared Secret authentifiziert, **ersetzt aber keine Verschlüsselung**. Das Modell wird **beim Start**
geladen: ein Dienst, der `/v1/health` bejaht und erst bei `/v1/detect` scheitert, würde Sluice
mitten im Egress-Pfad blockieren.

Der Sluice-**Kern** braucht weder `gliner` noch `torch` — er spricht den Dienst über HTTP an. Das
hält den Chokepoint selbst frei von Modell-Abhängigkeiten (Extras: `ner`, `ner-onnx`,
`ner-onnx-gpu`).

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
