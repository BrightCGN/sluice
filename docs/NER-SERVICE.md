# NER-Dienst & `pii_ner` — Betrieb, Messung, Evaluation

> **Stand:** 2026-08-07 · Spec-Revision 12 (§5.3, §5.4, §7.5)
> **Status der Deployment-Entscheidung (2026-08-10): CPU auf der Sluice-VM trägt nicht.**
> Proxy-Messung in [§3.1](#31-die-zwei-werte--stand-wert-2-gemessen-proxy-wert-1-offen):
> ~1,2 s bei 200 Zeichen, ~9,4 s bei 4.000 Zeichen — pro Request, im synchronen Egress-Pfad.
> Offen bleiben die Bestätigung auf der VM selbst und der ONNX/INT8-Pfad.

---

## 1. Was `pii_ner` ist

`pii_ner` = `pii_regex` **plus** Modellerkennung. Beide Stufen laufen, das Ergebnis ist
die **Vereinigungsmenge** der erkannten Spans. Der Modus ist **additiv, nicht alternativ**
— strukturell abgesichert: `PiiNerMode` ist eine *Unterklasse* von `PiiRegexMode`, die
Regex-Stufe kann also nicht wegfallen, ohne dass beide Modi brechen.

| Stufe | Zuständig für | Warum dort |
|---|---|---|
| **Regex** (`detectors/pii_de.py`) | IBAN (Mod-97), Steuer-ID, SVNR, KVNR, Kreditkarte (Luhn), E-Mail, IPv4/IPv6, MAC, KFZ-Kennzeichen, Telefon (deutsche Vorwahlstruktur) | Feste, prüfbare Form. Eine bestandene Prüfsumme *ist* die Typbestätigung — da rät nichts. |
| **NER** (`ner/`) | Personennamen, Organisationen, Freitext-Adressen, Ortsangaben, kontextabhängige Fälle | Kein festes Format; genau das, was Regex prinzipiell nicht kann. |

**Bei Überlappung gewinnt die Regex-Erkennung** (`spans.merge_spans`), weil nur sie den
validierten Typ kennt. Der Vorrang ist kategorisch, nicht längenabhängig: ein breiter
Modell-Span darf einen typisierten Platzhalter weder überschreiben noch zerschneiden.

Das Modell **ersetzt die Regex-Stufe nie**. Es gibt auch keinen generativen Pfad: ein LLM
wäre nichtdeterministisch, an Span-Grenzen halluzinationsanfällig und latenzseitig
untauglich für den synchronen Egress-Pfad.

---

## 2. Fail-closed

Ist der NER-Dienst nicht erreichbar oder reißt das Timeout-Budget, wird die Anfrage
**blockiert**. Kein stiller Rückfall auf `pii_regex`, keine Degradation — ein Chokepoint,
der bei Ausfall durchlässiger wird, ist kein Chokepoint.

Der Fehler geht **als solcher** an den Aufrufer und ins Audit:

| Lage | HTTP | `error.type` |
|---|---|---|
| Policy-Ablehnung (Profil, Allowlist, Verifier) | 403 | `sluice_blocked` |
| **NER-Dienst weg / Timeout / Identitätsabweichung** | **503** | **`sluice_mode_unavailable`** |

Die Trennung ist Absicht: eine Ablehnung ist endgültig, ein Ausfall ist ein
Betriebsvorfall — und er darf im 403-Rauschen nicht untergehen. Im Guard greift die Regel
generisch am Fehlertyp (`ModeUnavailableError`), nicht am Namen `pii_ner`; jeder künftige
Modus mit externer Abhängigkeit erbt das Verhalten.

Fail-closed greift auch bei **fehlender Konfiguration**: ohne `url` (bzw. `SLUICE_NER_URL`)
blockiert der Modus, statt die Stufe zu überspringen — dieselbe Härte wie die
Gateway-Pflicht aus Rev. 7.

---

## 3. Deployment: erst messen, dann entscheiden

### 3.1 Die zwei Werte — Stand: **Wert 2 gemessen (Proxy), Wert 1 offen**

| # | Frage | Stand |
|---|---|---|
| 1 | Enthält PyTorch Pascal-Kernel (`sm_61`)? | **offen** — nur auf dem GPU-Host beantwortbar |
| 2 | Reicht CPU-Inferenz? | **gemessen, Antwort: nein** (siehe unten) |

#### Messung vom 2026-08-10 (Proxy)

Gemessen **nicht** auf der Ziel-VM, sondern auf einer architektonisch nahen CPU. Das ist
zulässig, weil beide auf derselben Vektor-ISA-Stufe liegen — und weil das Design die
Inferenz ohnehin auf **einen** Thread festnagelt (§5.4), sodass die Kernzahl für einen
einzelnen Request keine Rolle spielt.

| | Messmaschine | Ziel-VM `sluicegw` |
|---|---|---|
| CPU | Intel i5-2410M, Sandy Bridge | Intel Xeon E3-1270 V2, Ivy Bridge |
| Takt | 2,30 GHz | 3,50 GHz |
| AVX / AVX2 / FMA | ja / **nein** / **nein** | ja / **nein** / **nein** |
| Kerne (genutzt) | 1 (gepinnt) | 1 vCPU |

Modell `urchade/gliner_multi_pii-v1` (mDeBERTa-v3-base, ~278M), PyTorch 2.13.0+cpu, fp32,
`OMP_NUM_THREADS=1`, Median aus 5 Läufen nach 2 Warmläufen:

| Eingabelänge | gemessen (2,3 GHz) | hochgerechnet Ziel-VM (÷1,5) | gefundene Spans |
|---|---|---|---|
| 200 Zeichen | 1.727 ms | **~1.150 ms** | 3 |
| 1.000 Zeichen | 5.217 ms | **~3.480 ms** | 14 |
| 4.000 Zeichen | 14.110 ms | **~9.400 ms** | 32 |

Hochrechnungsfaktor 1,5 = Taktverhältnis 3,5/2,3 plus ~5 % IPC-Gewinn Ivy über Sandy
Bridge, konservativ abgerundet für KVM-Overhead.

#### Bewertung: **CPU auf der VM trägt nicht**

Das sind Sekunden **pro Request**, im *synchronen* Egress-Pfad, **vor** dem Provider-Aufruf.
Selbst der günstigste Fall — Kurztext, kleineres Modell — liegt bei knapp einer Sekunde.
Der empfohlene Startpunkt `fastino/…` (~205M statt ~278M) bringt grob Faktor 0,74, also
~850 ms / ~2.600 ms / ~7.000 ms: dieselbe Größenordnung, dieselbe Antwort.

Zusätzlich: die VM hat **1 vCPU**, den sich Sluice-Kern, vier Gateway-Prozesse und der
NER-Dienst teilen. Während einer Inferenz ist der Kern belegt — es warten nicht nur der
eigene Request, sondern alle parallelen.

#### Was daran noch zu drehen wäre

1. **ONNX Runtime + INT8** (`knowledgator/gliner-pii-base-v1.0` hat fertige Exporte).
   Auf CPU üblich Faktor 2–4; ohne AVX2/VNNI realistisch eher 1,5–2,5. Damit bliebe man
   bei 4.000 Zeichen im Bereich mehrerer Sekunden. **Ungemessen** — der nächste Schritt,
   falls die VM-Variante trotzdem verfolgt werden soll.
2. **Feste Thread-Zahl > 1.** Die Determinismus-Zusage verlangt eine *fixierte und
   deklarierte* Thread-Zahl, nicht zwingend die 1 (§5.4). Ein fest auf z. B. 4 gepinntes
   `OMP_NUM_THREADS` wäre ebenso reproduzierbar — **sofern** verifiziert. Das brächte auf
   einer 4-vCPU-VM grob Faktor 2,5–3 und müsste dann Teil der Anonymisierungs-Identität
   werden. Vor Einsatz mit Wiederholungsläufen prüfen, nicht annehmen.
3. **GPU-Host.** Auf der GTX 1080 Ti liegt GLiNER bei Millisekunden statt Sekunden — zwei
   Größenordnungen. Preis: der Rohtext verlässt den Sluice-Host (§5a).
4. **`pii_regex` statt `pii_ner`, wo es reicht.** Die Regex-Stufe kostet Mikrosekunden und
   braucht keinen Dienst. Die Zweistufigkeit ist pro Profil wählbar — genau dafür.

> **Der definitive Wert kommt weiterhin von der VM selbst.** `scripts/probe_ner_hardware.py`
> läuft dort jetzt ohne Klimmzüge (der `sys.path`-Bootstrap ist drin). Die Proxy-Zahlen
> ersetzen die Messung nicht, sie machen ihren Ausgang nur sehr wahrscheinlich.

### 3.2 Messung durchführen

```bash
# Wert 1 — Umgebung, braucht weder Modell noch Netz:
python3 scripts/probe_ner_hardware.py

# Wert 2 — Latenz bei 200/1000/4000 Zeichen, Median über 5 Läufe nach 2 Warmläufen:
python3 scripts/probe_ner_hardware.py \
    --model fastino/gliner2-privacy-filter-PII-multi \
    --json docs/messwerte-ner.json
```

Das Skript bewertet mit: fehlt `sm_61` in `torch.cuda.get_arch_list()`, gibt es auf der
1080 Ti keinen PyTorch-CUDA-Pfad — dann ONNX Runtime mit CUDA-Provider statt
`SLUICE_NER_DEVICE=cuda`.

### 3.3 Entscheidungsregel (steht fest, die Zahlen fehlen)

1. **CPU reicht ⇒ CPU.** Ein Chokepoint ohne GPU-Abhängigkeit ist betrieblich robuster.
   Bei ~205M Parametern und kurzen Texten ist das plausibel — aber ungemessen bleibt es
   eine Vermutung, besonders auf einer CPU ohne AVX2.
2. **CPU zu langsam ⇒ GPU prüfen.** Seit Qwen3 der GLiNER-Installation weicht, ist die
   Karte praktisch frei (~11 GB statt ~5 GB) — die Koexistenz-Frage stellt sich nur noch,
   falls dort später wieder ein zweites Modell einzieht. Der FX-6300 **ohne AVX2** macht
   den GPU-Pfad hier eher wahrscheinlich als bei moderner CPU: die ONNX-/PyTorch-CPU-Kernel
   fallen ohne AVX2 auf deutlich langsamere Pfade zurück. Trotzdem gilt die Reihenfolge —
   erst messen.
3. **GPU nötig, aber `sm_61` fehlt ⇒ ONNX Runtime + CUDA-Provider**
   (`SLUICE_NER_ONNX=1`, `extras: ner-onnx-gpu`).

**Die Messwerte gehören anschließend hierher**, in eine Tabelle unter 3.1, zusammen mit
dem gewählten Backend und dem Datum. Erst dann ist das Akzeptanzkriterium „Deployment-
Entscheidung dokumentiert" erfüllt.

---

## 4. Modellkandidaten

Kein Modell ist vorab festgelegt; **mindestens zwei** sind gegeneinander zu evaluieren,
deutsche Sprachabdeckung ist zwingend.

| Modell | Anmerkung |
|---|---|
| `fastino/gliner2-privacy-filter-PII-multi` | GLiNER2-PII, ~205M Parameter, 42 Entitätstypen, 7 Sprachen. Bester Recall unter den GLiNER-Detektoren. **Empfohlener Startpunkt.** |
| `knowledgator/gliner-pii-base-v1.0` | 60+ Kategorien, quantisierungsbewusst trainiert, fertige ONNX-Exporte in FP16 und UINT8 — der bequemste ONNX-Pfad. |
| `urchade/gliner_multi_pii-v1` | Referenzimplementierung, 6 Sprachen inkl. Deutsch. |
| `VAGOsolutions/SauerkrautLM-GLiNER` | DE/EN/IT/FR/ES gemeinsam trainiert, deutscher Benchmark eigens kuratiert. Allzweck-NER, **nicht** PII-spezialisiert — als Kontrast gegen die PII-Modelle nützlich. |

GLiNER nimmt die Entitätstypen **zur Laufzeit** als Label-Liste entgegen. Deshalb steht die
Liste in der Sluice-Konfiguration (`[profile.X.ner] labels`) und ist Teil der versionierten
Anonymisierungs-Identität — nicht im Dienst verdrahtet. Ein Modelltausch braucht damit
keinen Code-Eingriff.

---

## 5. Evaluation & Schwellwert

> **Status: nicht kalibriert.** Es liegt kein deutschsprachiger Evaluationsdatensatz vor,
> und ohne ihn ist der Schwellwert nicht kalibrierbar. Der ausgelieferte Wert
> (`DEFAULT_THRESHOLD = 0.30`) ist ein **recall-orientierter Startwert**, keine
> Kalibrierung. Profile, die ihn erben, weisen das in der Identität als
> `threshold_calibrated: false` aus — sichtbar, statt still.

### 5.1 Recall vor Precision

Der Schwellwert wird auf **Recall optimiert, nicht auf F1**. Das weicht bewusst von der
üblichen Modellkalibrierung ab: F1 gewichtet einen zusätzlichen False Positive genauso wie
einen übersehenen Span. Für eine Egress-Boundary stimmt diese Gewichtung nicht —
Übermaskierung kostet Nutzen, ein übersehener Span kostet die Zusage.

`scripts/eval_ner.py` sucht deshalb den **höchsten** Schwellwert, der das Recall-Ziel noch
hält, und zwar **auf dem schlechtesten Entitätstyp**, nicht im Durchschnitt: ein Modell mit
guter Makro-Zahl kann bei einem einzelnen kritischen Typ versagen.

### 5.2 Datensatz

Erforderlich ist ein deutschsprachiger Datensatz, **möglichst aus dem realen
Anwendungsfeld** — generische Benchmarks überschätzen den Recall auf Haushalts- und
Kanzleitexten regelmäßig. Format (JSONL):

```json
{"text": "Richard Cochius wohnt in Köln.", "spans": [{"start": 0, "end": 15, "label": "person"}]}
```

Zwei getrennte Splits: **`dev` zum Kalibrieren, `test` zum Berichten.** Auf demselben
Split zu tunen und zu berichten überschätzt die Güte systematisch.

### 5.3 Durchführung

```bash
python3 scripts/eval_ner.py \
    --dev  docs/eval/de-dev.jsonl \
    --test docs/eval/de-test.jsonl \
    --model fastino/gliner2-privacy-filter-PII-multi \
    --model urchade/gliner_multi_pii-v1 \
    --target-recall 0.98 \
    --json docs/eval/ergebnis.json
```

Ausgewiesen werden Span-Level-Metriken **getrennt nach Entitätstyp**, inklusive Recall pro
Typ und dem schlechtesten Typ. Erreicht kein Schwellwert das Ziel, **warnt** das Skript und
wählt bewusst *nichts* — statt stillschweigend auf einen F1-Wert auszuweichen.

Der gewählte Wert gehört danach ins Profil (`[profile.X.ner] threshold`), zusammen mit
`model_repo` und `model_revision`. **Die Ergebnisse gehören hierher**, als Tabelle je
Modell und Entitätstyp.

---

## 5a. Transport: NER-Dienst auf eigenem Host

Der NER-Dienst ist die **einzige** Komponente im System, die *unsanitisierten* Rohtext
sieht — das ist sein Zweck, er soll die PII ja finden, bevor redigiert wird. Daraus folgt
etwas, das man leicht übersieht:

> **Der NER-Hop trägt mehr Personenbezug als der Provider-Hop.** Zum Provider geht nur noch
> Sanitisiertes, und zwar über TLS. Zum NER-Dienst geht alles, unredigiert. Ein
> Klartext-Transport genau hier wäre die schwächste Stelle einer Kette, die sonst überall
> fail-closed ist.

Solange der Dienst auf `127.0.0.1` läuft, ist das kein Thema — der Rohtext verlässt den Host
nie. Sobald er auf einem eigenen Rechner liegt (etwa weil dort die GPU steckt), gilt:

| Lage | Verhalten |
|---|---|
| `http://` + Loopback | frei |
| `https://` + beliebiger Host | frei |
| `http://` + entfernter Host | **blockiert**, bis `SLUICE_NER_ALLOW_PLAINTEXT_REMOTE=1` |

Die Schranke greift fail-closed wie alles andere: Vergessen heißt zu, nicht offen. Sie ist
bewusst als *Opt-in* gebaut und nicht als hartes Verbot — Sluice kann einen WireGuard-Tunnel
unter dem `http://` nicht sehen und würde einen korrekt abgesicherten Aufbau sonst
fälschlich blockieren.

**Empfohlene Reihenfolge:**

1. **NER-Dienst auf demselben Host wie der Sluice-Kern.** Löst das Problem, statt es zu
   verwalten. Nur wenn die GPU woanders steckt, lohnt der Aufwand darunter.
2. **WireGuard** zwischen Kern und NER-Host. URL bleibt `http://`, der Kern braucht dann
   `SLUICE_NER_ALLOW_PLAINTEXT_REMOTE=1` — der Tunnel ist für Sluice unsichtbar.
3. **TLS davor** (nginx/stunnel), URL wird `https://`. Dann greift die Schranke gar nicht.
4. **Klartext im isolierten Segment** — nur mit ausdrücklichem Opt-in, und dann bitte
   bewusst.

In allen entfernten Varianten zusätzlich: `SLUICE_NER_TOKEN` setzen (authentifiziert,
**ersetzt keine Verschlüsselung**) und Port 17900 per Firewall auf die Kern-IP beschränken.

---

## 6. Determinismus

Verifizierbare Anonymisierung setzt voraus, dass identische Eingaben identische Spans
liefern. Getragen wird das von vier Dingen:

| Maßnahme | Wo | Wirkung |
|---|---|---|
| **Batchgröße fixiert auf 1** | `FIXED_BATCH_SIZE`, Dienst lehnt abweichende ab (400) | Kein dynamisches Batching ⇒ die Reduktionsreihenfolge in Gleitkommaoperationen variiert nicht mit der Auslastung; Grenzfälle am Schwellwert kippen nicht zwischen Läufen. |
| **Ein Intra-Op-Thread** | `engine._pin_determinism`, `OMP_NUM_THREADS=1` | Gleicher Grund: Thread-Anzahl ändert die Reduktionsreihenfolge. |
| **Präzision fixiert & ausgewiesen** | `SLUICE_NER_PRECISION` → `/v1/info` → Identität | Ein Wechsel fp32→fp16 verschiebt Scores und damit Grenzfälle — er darf nicht unbemerkt passieren. |
| **Totale Sortierordnung** | `spans._order` | Zwei gleich lange Spans an derselben Stelle wären sonst nur zufällig geordnet. |

Der **Inhalts-Hash-Cache** (`ner/client.py`) macht *Wiederholungen* bitgleich und senkt die
Latenz. Über Prozessneustarts hinweg trägt er die Zusage **nicht** — das tun die vier Punkte
oben. Der Cache-Schlüssel enthält Schwellwert, Labels und Modellidentität; sonst überlebte
ein alter Eintrag eine Konfigurationsänderung und würde genau die Identität aushebeln, die
er stabilisieren soll.

---

## 7. Anonymisierungs-Identität

Beantwortet die Frage *„womit genau wurde dieser Text anonymisiert?"* — die Voraussetzung
dafür, dass „anonymisiert" im Audit mehr ist als eine Behauptung.

```bash
curl 'http://127.0.0.1:17800/v1/anonymization-identity?profile=prismclaw-ner'
```

```json
{
  "profile": "prismclaw-ner",
  "digest": "3cd8ad4c0aa4c55a…",
  "mode": "pii_ner",
  "detector_profile": "pii_de",
  "dictionary_digest": "none",
  "ner": {
    "model_repo": "fastino/gliner2-privacy-filter-PII-multi",
    "model_revision": "…",
    "model_precision": "fp32",
    "threshold": 0.30,
    "labels": ["person", "organization", "address", "location"],
    "batch_size": 1,
    "threshold_calibrated": false
  }
}
```

Modellversion und Schwellwert sind daraus direkt ableitbar. Ändert sich eines von beidem,
ändert sich der Digest — ein stiller Modellwechsel wird im Audit sichtbar. Die
Wörterbuch-Terme gehen nur als **Digest** ein: sie sind personenbezogen (Rev. 10) und
dürfen nie ins Audit, ihre Änderung muss aber sichtbar sein.

Meldet der Dienst über `/v1/info` eine **andere** Identität als das Profil verankert,
blockiert Sluice fail-closed. Sonst wäre der protokollierte Wert eine Lüge.

> `model_revision` leer zu lassen macht die Identität wertlos: das Repo könnte sich unter
> derselben Kennung ändern. Commit-Hash eintragen.

---

## 8. Betrieb des Dienstes

Der NER-Dienst ist ein **eigenständiger Prozess mit bewusst schmaler Schnittstelle**: Text
rein, Spans raus. **Nicht** in ihm: Pseudonym-Zuordnung, Modus-Schalter, Profilbindung,
Maskierungslogik, Schwellwert-Anwendung. Das alles bleibt in Sluice — erst diese Enge macht
das Modell austauschbar und erlaubt den Vergleichsbetrieb zweier Modelle, **ohne den
Chokepoint zu duplizieren**.

```
GET  /v1/health  (= /health)   → {"status":"ok","model":"…"}
GET  /v1/info    (= /info)     → {model, revision, precision, labels, backend, score_floor, batch_size}
POST /v1/detect  (= /detect)   → {"text":"…","labels":[…]} → {"spans":[{start,end,label,score}]}
```

`start`/`end` sind **Zeichen**-Offsets, nie Token-Offsets. Die versionierten Pfade sind der
Vertrag (§7.4); die unversionierten bleiben als Alias bestehen.

### Installation

```bash
useradd --system --home /opt/sluice --shell /usr/sbin/nologin sluice-ner
/opt/sluice/.venv/bin/pip install '.[ner]'        # bzw. '.[ner-onnx]' / '.[ner-onnx-gpu]'

install -m600 -o root deploy/ner.env.example /etc/sluice/ner.env
$EDITOR /etc/sluice/ner.env                       # Modell + REVISION eintragen
install -m644 deploy/sluice-ner.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now sluice-ner

curl http://127.0.0.1:17900/v1/health
curl http://127.0.0.1:17900/v1/info
```

Wie die Provider-Gateways (Rev. 6/8) ist das ein **interner** Dienst: eigener System-User,
eingehend nur vom Sluice-Kern (Firewall), optional Shared Secret `SLUICE_NER_TOKEN`. Er
sieht Rohtext — für ihn gilt dieselbe Netz-Regel wie für den Kern, nicht die lockere eines
Hilfsdienstes.

Das Modell wird **beim Start** geladen, nicht beim ersten Request: ein Dienst, der
`/v1/health` bejaht und dann bei `/v1/detect` scheitert, würde Sluice erst mitten im
Egress-Pfad blockieren. `TimeoutStartSec=300`, weil das Laden dauert.

---

## 9. Offene Punkte

| Punkt | Was fehlt |
|---|---|
| **Deployment-Entscheidung CPU/GPU** | Messung auf `192.168.101.166` (§3.2), Zahlen nach §3.1 eintragen. |
| **Qwen3 → GLiNER** | Der `llama-server` auf dem Zielhost wird durch den NER-Dienst ersetzt. Vorher prüfen, ob Qwen3 dort noch andere Konsumenten hat. |
| **Schwellwert-Kalibrierung** | Deutschsprachiger Dev/Test-Split aus dem realen Anwendungsfeld; danach `scripts/eval_ner.py` und Ergebnis nach §5. |
| **Modellauswahl** | Mindestens zwei Kandidaten gegeneinander (§4). Bis dahin ist der Startpunkt eine Empfehlung, keine Wahl. |
| **`model_revision`** | In `ner.env` und im Profil eintragen — solange leer, ist die Identität nicht festgenagelt. |
| **Transport bei entferntem Betrieb** | Tunnel oder TLS wählen (§5a), `SLUICE_NER_TOKEN` setzen, Port 17900 auf die Kern-IP beschränken. |
| **Feinschliff `pii_de`** | KFZ- und Telefonmuster sind recall-orientiert weit gefasst; die False-Positive-Rate auf echten Texten ist noch nicht vermessen. |
