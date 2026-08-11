# NER-Dienst & `pii_ner` — Betrieb, Messung, Evaluation

> **Stand:** 2026-08-11 · Spec-Revision 12 (§5.3, §5.4, §7.5)
> **Status der Deployment-Entscheidung: CPU auf der Sluice-VM trägt nicht — auf der VM
> selbst bestätigt** (2026-08-11, zwei unabhängige Läufe): **1,24 s** bei 200 Zeichen,
> **8,6 s** bei 4.000 — pro Request, im *synchronen* Egress-Pfad, vor dem Provider-Aufruf.
> **ONNX Runtime trägt für kurze Texte** (173 ms bei 200 Zeichen); **Quantisierung nicht**
> — ohne VNNI ist uint8 rund 18 % *langsamer* als fp32. Offen sind Modellwahl nach Recall
> und die Schwellwert-Kalibrierung; beides braucht einen Dev-Split (`NER-EVAL-SPLIT.md`).

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
| **Modell hat die Eingabe gekürzt** (§2a) | **503** | **`sluice_mode_unavailable`** |

Die Trennung ist Absicht: eine Ablehnung ist endgültig, ein Ausfall ist ein
Betriebsvorfall — und er darf im 403-Rauschen nicht untergehen. Im Guard greift die Regel
generisch am Fehlertyp (`ModeUnavailableError`), nicht am Namen `pii_ner`; jeder künftige
Modus mit externer Abhängigkeit erbt das Verhalten.

Fail-closed greift auch bei **fehlender Konfiguration**: ohne `url` (bzw. `SLUICE_NER_URL`)
blockiert der Modus, statt die Stufe zu überspringen — dieselbe Härte wie die
Gateway-Pflicht aus Rev. 7.

---

## 2a. Lange Texte: Kürzung ist die gefährlichste Lücke

GLiNER-Modelle haben ein festes Token-Fenster — bei `urchade/gliner_multi_pii-v1` sind es
**384 Token** — und kürzen längere Eingaben **still** darauf. Kein Fehler, keine Ausnahme,
nur eine `UserWarning` im Log der Bibliothek.

Auf der Sluice-VM gemessen (2026-08-10):

```
Text: 6.304 Zeichen, ein Name im ersten Satz, einer im letzten
gefunden: ['Anna Schmidt']          ← der Name am Ende fehlt
UserWarning: Sentence of length 973 has been truncated to 384
```

**Warum das schlimmer ist als ein Ausfall:** Ein Ausfall blockiert (§2) — die Anfrage
kommt nicht durch, jemand merkt es. Eine Kürzung *lässt durch*: die Erkennung liefert
Spans für den vorderen Teil, Sluice hält den Text für geprüft und protokolliert
`released=true`. Der ungeprüfte Rest geht ungeschwärzt zum Provider. Ein Chokepoint, der
den halben Text nicht ansieht und trotzdem zusagt, ist keiner.

Zwei Ebenen dagegen, in dieser Reihenfolge:

**1. Das Netz darunter — der Dienst blockiert bei tatsächlicher Kürzung.**
`GlinerEngine.detect` erhebt genau diese Warnung zur Ausnahme (`NerTruncationError`); der
Dienst antwortet mit **413 `ner_text_truncated`**, der Client übersetzt das in
`ModeUnavailableError` ⇒ **503**. Die Prüfung sitzt am *tatsächlichen Ereignis*, nicht an
einer geschätzten Längenschranke — sie gilt damit für jedes Modell und jeden Tokenizer und
lässt sich durch eine falsch dimensionierte Zerlegung nicht umgehen.

**2. Der reguläre Weg — der Client zerlegt vorher.** Lange Texte werden in überlappende
Stücke geschnitten (`max_chars_per_chunk`, `chunk_overlap_chars`), jedes Stück einzeln
erkannt, die Offsets auf den Originaltext zurückgerechnet und die Ergebnisse vereinigt.
Ebene 1 greift damit im Normalbetrieb nie.

| Parameter | Default | Warum so |
|---|---|---|
| `max_chars_per_chunk` | 700 | Passt auch im ungünstigsten Fall in ein 384-Token-Fenster. Sluice vergleicht den Wert beim ersten Kontakt mit dem gemeldeten `max_tokens` und **blockiert**, wenn er nicht hineinpasst. |
| `chunk_overlap_chars` | 200 | Eine Entität an der Schnittstelle wäre sonst in beiden Stücken nur halb enthalten und würde in beiden verfehlt. Muss länger sein als die längste erwartete Entität. |

Beide sind **Konfiguration und Teil der Anonymisierungs-Identität** (§5.4) — nicht aus dem
Dienst abgeleitet. Andere Schnitte bedeuten anderen Kontext je Stück und damit andere
Spans; würde die Stückgröße aus `/v1/info` übernommen, verschöbe ein Modellwechsel sie
still und „gleicher Digest ⇒ gleiche Spans" wäre für lange Texte unwahr.

**Preis:** Ein Text von 4.000 Zeichen wird zu etwa 8 Stücken, also 8 Modellaufrufen. Bei
den gemessenen ~1,2 s für kurze Eingaben summiert sich das — die Zerlegung macht die
Latenzfrage aus §3 nicht besser, sondern schärfer. Sie ist trotzdem nicht verhandelbar:
die Alternative ist kein schnellerer Riegel, sondern gar keiner.

Dubletten aus dem Überlappungsbereich werden zusammengefasst; bei unterschiedlichem Score
gewinnt der **höhere** (die Stufe ist recall-orientiert, §5.4). Echte Überlappungen
verschiedener Spans bleiben stehen und werden erst in `spans.merge_spans` aufgelöst — dort,
wo auch der Vorrang der Regex-Stufe entschieden wird.

---

## 3. Deployment: erst messen, dann entscheiden

### 3.1 Die zwei Werte — Stand: **Wert 2 auf der VM gemessen, Wert 1 offen**

| # | Frage | Stand |
|---|---|---|
| 1 | Enthält PyTorch Pascal-Kernel (`sm_61`)? | **offen** — nur auf dem GPU-Host beantwortbar |
| 2 | Reicht CPU-Inferenz? | **auf der VM gemessen, Antwort: nein** (siehe unten) |

#### Messung auf der Ziel-VM, 2026-08-11 — maßgeblich

Zwei unabhängige Läufe auf `sluicegw` selbst, dazwischen eine Tuning-Runde am
Proxmox-Host. Modell `urchade/gliner_multi_pii-v1` (mDeBERTa-v3-base, ~278M),
PyTorch 2.13.0+cpu, fp32, ein Thread, Median aus 5 Läufen nach 2 Warmläufen:

| Eingabelänge | 1. Lauf | 2. Lauf (nach Host-Tuning) | Spans |
|---|---|---|---|
| 200 Zeichen | 1.243,9 ms | **1.242,8 ms** | 3 |
| 1.000 Zeichen | 3.853,8 ms | **3.835,6 ms** | 14 |
| 4.000 Zeichen | 8.651,3 ms | **8.597,5 ms** | 32 |

Ladezeit 75–77 s (passt in `TimeoutStartSec=300`). Streuung innerhalb eines Laufs
< 1 % — das Ergebnis ist stabil, nicht zufällig.

**Das Host-Tuning hat nichts bewegt** (< 1 % Unterschied, also Rauschen), und das ist
kein Versäumnis am Hypervisor: die Erkennung läuft auf **einem** festgenagelten Thread
(§5.4), der Engpass ist die Rechenleistung *eines* Kerns auf einer Ivy-Bridge-CPU ohne
AVX2. vCPU-Zahl, Ballooning, NUMA und Scheduler verteilen Arbeit, sie beschleunigen sie
nicht. Messbar würde nur, was Befehlssatz oder Takt bewegt — beides liegt hier fest.

**Die Proxy-Hochrechnung war gut.** Sie sagte ~1.150 ms / ~9.400 ms voraus; gemessen
wurden 1.243 ms / 8.598 ms. Die Methode taugt also für künftige Abschätzungen — die
Entscheidung stützt sich trotzdem auf die Messung auf der Zielmaschine.

<details>
<summary>Frühere Proxy-Messung vom 2026-08-10 (überholt, zur Nachvollziehbarkeit)</summary>

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

</details>

#### Bewertung: **CPU auf der VM trägt nicht**

Das sind Sekunden **pro Request**, im *synchronen* Egress-Pfad, **vor** dem Provider-Aufruf.
Selbst der günstigste Fall — Kurztext — liegt bei knapp einer Sekunde. Ein kleineres Modell
verschiebt das proportional zur Parameterzahl, also um einen Faktor und nicht um eine
Größenordnung: dieselbe Antwort. (Der zuvor hier als Ausweg genannte `fastino/…`-Checkpoint
ist ohnehin nicht ladbar, siehe §4.)

Zusätzlich: die VM hat **1 vCPU**, den sich Sluice-Kern, vier Gateway-Prozesse und der
NER-Dienst teilen. Während einer Inferenz ist der Kern belegt — es warten nicht nur der
eigene Request, sondern alle parallelen.

#### ONNX Runtime: gemessen am 2026-08-11 — und INT8 ist der falsche Weg

`knowledgator/gliner-pii-base-v1.0`, drei Exporte, dieselbe VM, gleiche Methode:

| Eingabelänge | torch fp32 (`urchade`, ~278M) | **ONNX fp32** | ONNX quint8 |
|---|---|---|---|
| 200 Zeichen | 1.243 ms | **173 ms** | 204 ms |
| 1.000 Zeichen | 3.836 ms | **826 ms** | 990 ms |
| 4.000 Zeichen | 8.598 ms | **4.387 ms** | 5.141 ms |
| Ladezeit | 75 s | 13 s | 5 s |

**Quantisierung macht es hier langsamer, nicht schneller** — durchgehend etwa 18 %. Das
widerlegt die frühere Annahme („Faktor 2–4, ohne AVX2 eher 1,5–2,5") und ist auf dieser
CPU plausibel: INT8-Gewinne stammen aus VNNI-Befehlen, die es erst ab Cascade Lake gibt.
Ohne sie bleibt vom INT8-Pfad nur der Aufwand fürs Quantisieren und Dequantisieren.
**Auf Hardware ohne VNNI also fp32 über ONNX, nicht uint8.**

Der Sprung von 1.243 auf 173 ms ist Faktor 7,2 — aber er mischt zwei Ursachen: die
Runtime *und* das kleinere Modell. Trennen lässt sich das hier nicht, weil `urchade`
keine ONNX-Exporte mitbringt. Für die Deployment-Entscheidung ist es gleich, für die
Begründung nicht.

**Die Exporte liefern unterschiedliche Ergebnisse** — 89 Spans (`model.onnx`) gegen 113
(`model_quint8.onnx`) bei 4.000 Zeichen. Die geladene Datei entscheidet also über die
Erkennung und ist damit identitätsrelevant (§5.4). Ob die 113 mehr Funde oder mehr
Fehlalarme sind, sagt keine Latenzmessung, sondern `eval_ner.py`.

#### Was daran noch zu drehen wäre
2. **Feste Thread-Zahl > 1.** Die Determinismus-Zusage verlangt eine *fixierte und
   deklarierte* Thread-Zahl, nicht zwingend die 1 (§5.4). Ein fest auf z. B. 4 gepinntes
   `OMP_NUM_THREADS` wäre ebenso reproduzierbar — **sofern** verifiziert. Das brächte auf
   einer 4-vCPU-VM grob Faktor 2,5–3 und müsste dann Teil der Anonymisierungs-Identität
   werden. Vor Einsatz mit Wiederholungsläufen prüfen, nicht annehmen.
3. **GPU-Host.** Auf der GTX 1080 Ti liegt GLiNER bei Millisekunden statt Sekunden — zwei
   Größenordnungen. Preis: der Rohtext verlässt den Sluice-Host (§5a).
4. **`pii_regex` statt `pii_ner`, wo es reicht.** Die Regex-Stufe kostet Mikrosekunden und
   braucht keinen Dienst. Die Zweistufigkeit ist pro Profil wählbar — genau dafür.

> **Erledigt:** Die Messung auf der VM liegt vor (oben) und bestätigt die Hochrechnung.
> Damit ist Wert 2 abschließend beantwortet; offen ist nur noch der ONNX/INT8-Pfad.

### 3.2 Messung durchführen

```bash
# Wert 1 — Umgebung, braucht weder Modell noch Netz:
python3 scripts/probe_ner_hardware.py

# Wert 2 — Latenz bei 200/1000/4000 Zeichen, Median über 5 Läufe nach 2 Warmläufen:
python3 scripts/probe_ner_hardware.py \
    --model urchade/gliner_multi_pii-v1 \
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
| `urchade/gliner_multi_pii-v1` | Referenzimplementierung (mDeBERTa-v3-base, ~278M), 6 Sprachen inkl. Deutsch. Lädt mit dem `gliner`-Paket. **Empfohlener Startpunkt.** |
| `knowledgator/gliner-pii-base-v1.0` | 60+ Kategorien, quantisierungsbewusst trainiert, fertige ONNX-Exporte in FP16 und UINT8 — der bequemste ONNX-Pfad. |
| `VAGOsolutions/SauerkrautLM-GLiNER` | DE/EN/IT/FR/ES gemeinsam trainiert, deutscher Benchmark eigens kuratiert. Allzweck-NER, **nicht** PII-spezialisiert — als Kontrast gegen die PII-Modelle nützlich. |
| ~~`fastino/gliner2-privacy-filter-PII-multi`~~ | **Nicht verwendbar** — am 2026-08-10 auf der VM verifiziert. Es ist ein **GLiNER2**-Checkpoint: im Snapshot liegen `config.json` und `encoder_config/`, aber keine `gliner_config.json`, und `GLiNER.from_pretrained` bricht mit `No config file found` ab. `gliner` 0.2.28 exportiert nur `GLiNER`/`GLiNERConfig`, keine GLiNER2-Klasse. Es bräuchte die separate `gliner2`-Bibliothek und damit einen zweiten Engine-Adapter. Stand hier zuvor als „empfohlener Startpunkt" — das war aus der Modellbeschreibung übernommen, nicht geprüft. |

**Lehre daraus, für jeden weiteren Kandidaten:** vor jeder Empfehlung einmal
`GLiNER.from_pretrained(<repo>)` laufen lassen. Parametergröße und Entitätstypen aus der
Modellkarte sagen nichts darüber, ob der Checkpoint zum installierten Paket passt.

### 4.1 Pflichtprüfung vor jedem Modellwechsel: stimmen die Offsets?

Sluice redigiert über **Zeichen-Offsets**, die das Modell liefert (§5.3). Stimmen die
nicht, ist der Schaden subtil und groß zugleich: die Erkennung *funktioniert*, sie meldet
die richtigen Namen — nur die Koordinaten sind verschoben. Herauskommt ein Text, in dem
der Platzhalter neben dem Namen steht und der Name selbst stehen bleibt. Der Verifier
fängt davon nur, was er als Muster kennt; freie Namen fielen durch.

Typische Ursache wäre ein Modell, das Byte- statt Zeichen-Offsets liefert. Auffallen würde
das erst ab dem ersten Mehrbyte-Zeichen — im deutschen Text also spätestens beim ersten
Umlaut. Deshalb gehören Umlaute, `ß` **und** ein Zeichen außerhalb der BMP in den Testtext:

```bash
sudo -u sluice-ner HF_HOME=/opt/sluice/models HF_HUB_OFFLINE=1 \
  /opt/sluice/.venv/bin/python - <<'PY'
from gliner import GLiNER
TEXT = ("Am Montag traf Anna Schmidt die Firma Acme GmbH in Köln. "
        "Ihre Kollegin Bärbel Müßiggang wohnt in der Grüne Straße 7. "
        "🏠 Danach fuhr Bernd Mueller nach Düsseldorf.")
modell = GLiNER.from_pretrained("<repo>")
schlecht = 0
for e in modell.predict_entities(TEXT, ["person","organization","address","location"], threshold=0.3):
    ok = TEXT[e["start"]:e["end"]] == e["text"]
    schlecht += not ok
    print(f'{e["label"]:13} {e["text"]!r:22} {"OK" if ok else "ABWEICHUNG"}')
print("belastbar" if not schlecht else "OFFSETS KAPUTT")
PY
```

**Ergebnis 2026-08-11:** `urchade/gliner_multi_pii-v1` 7 Spans / 0 Abweichungen,
`knowledgator/gliner-pii-base-v1.0` 8 Spans / 0 Abweichungen. Beide liefern korrekte
Zeichen-Offsets. Die Warnung, die `transformers` beim Laden von `microsoft/mdeberta-v3-base`
ausgibt („incorrect tokenization"), betrifft die Tokenisierung *innerhalb* des Modells und
ist für die zurückgegebenen Koordinaten folgenlos — geprüft, nicht angenommen.

Nebenbefund zur Modellwahl: `knowledgator` hat das Emoji als `location` klassifiziert.
Ein Fehlalarm, passend zu den 113 statt 89 Spans aus §3.1 — das Modell ist großzügiger.
Für einen recall-orientierten Riegel (§5.4) ist Übermaskierung die billigere Fehlerart,
aber es ist ein Hinweis auf geringere Precision. Ein Datenpunkt, keine Bewertung.

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

**Der Datensatz gehört NICHT ins Repo** — er besteht aus echten personenbezogenen Daten,
und ein einmal committeter Bestand ist über den Git-Verlauf nicht mehr zu entfernen.
Ablage unter `/var/lib/sluice-eval` (0700). Wie der Split entsteht — Umfang, Auswahl,
Annotationsregeln, Qualitätsprüfung —: **`NER-EVAL-SPLIT.md`**.

### 5.3 Durchführung

```bash
python3 scripts/eval_ner.py \
    --dev  /var/lib/sluice-eval/de-dev.jsonl \
    --test /var/lib/sluice-eval/de-test.jsonl \
    --model urchade/gliner_multi_pii-v1 \
    --model knowledgator/gliner-pii-base-v1.0 \
    --target-recall 0.98 \
    --json /var/lib/sluice-eval/ergebnis.json
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
    "model_repo": "urchade/gliner_multi_pii-v1",
    "model_revision": "…",
    "model_precision": "fp32",
    "threshold": 0.30,
    "labels": ["person", "organization", "address", "location"],
    "batch_size": 1,
    "threshold_calibrated": false,
    "max_chars_per_chunk": 700,
    "chunk_overlap_chars": 200
  }
}
```

Modellversion und Schwellwert sind daraus direkt ableitbar. Ändert sich eines von beidem,
ändert sich der Digest — ein stiller Modellwechsel wird im Audit sichtbar. Die
Wörterbuch-Terme gehen nur als **Digest** ein: sie sind personenbezogen (Rev. 10) und
dürfen nie ins Audit, ihre Änderung muss aber sichtbar sein.

Die **Zerlegungsparameter** stehen mit drin, weil sie das Ergebnis verändern: andere
Schnitte heißen anderer Kontext je Stück und damit andere Spans (§2a). Ohne sie wäre
„gleicher Digest ⇒ gleiche Spans" für lange Texte schlicht unwahr.

Drei Schranken halten die Identität ehrlich, alle fail-closed:

| Prüfung | Wogegen | Wann |
|---|---|---|
| Modellidentität | Profil verankert `model_repo`/`model_revision`/`model_precision` ≠ `/v1/info` | erster Kontakt |
| `score_floor` | Dienst filtert schärfer vor als der Profil-Schwellwert ⇒ Schwelle wirkungslos | erster Kontakt |
| Stückgröße vs. `max_tokens` | konfigurierte Stücke passen nicht ins Kontextfenster ⇒ stille Kürzung | erster Kontakt |
| ONNX-Präzision | `SLUICE_NER_PRECISION` ≠ geladener Export (Name **und** Graph-Inhalt) | Dienststart |

Die letzte kam am 2026-08-11 dazu. Anlass: `SLUICE_NER_PRECISION` wurde frei deklariert,
während `SLUICE_NER_ONNX_FILE` bestimmte, was wirklich lief — und die Exporte liefern
unterschiedliche Ergebnisse (89 vs. 113 Spans bei 4.000 Zeichen). Der Dienst leitet die
Präzision jetzt aus dem Exportnamen ab, sieht zusätzlich im Graph nach
Quantisierungs-Operatoren und **startet bei Widerspruch nicht**.

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
install -d -o sluice-ner -g sluice-ner -m 700 /opt/sluice/models

# torch ZUERST und allein aus dem CPU-Index. Sonst zieht `pip install gliner` die
# CUDA-Variante samt kompletter nvidia-Laufzeit nach — mehrere GB auf einer Maschine
# ohne GPU. Ist torch danach erfüllt, lässt gliner die nvidia-Pakete weg.
/opt/sluice/.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch
/opt/sluice/.venv/bin/pip install '/opt/sluice[ner]'   # bzw. [ner-onnx] / [ner-onnx-gpu]
/opt/sluice/.venv/bin/pip list | grep -i -E 'nvidia|cuda' || echo "keine CUDA-Pakete"

# pip lief als root und hat Dateien neu geschrieben — Rechte danach geradeziehen,
# und den Modell-Cache erst NACH dem rekursiven chmod auf 700 setzen.
chown -R sluice:sluice /opt/sluice && chmod -R a+rX /opt/sluice
chown -R sluice-ner:sluice-ner /opt/sluice/models && chmod -R go-rwx /opt/sluice/models

install -m600 -o root deploy/ner.env.example /etc/sluice/ner.env
$EDITOR /etc/sluice/ner.env                       # Modell + REVISION eintragen
install -m644 deploy/sluice-ner.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now sluice-ner

curl http://127.0.0.1:17900/v1/health
curl http://127.0.0.1:17900/v1/info
```

**Modell vorab und getrennt laden.** Der erste Dienststart würde es sonst mitten im
`TimeoutStartSec`-Fenster holen, und ein Abbruch kostet dann den ganzen Startversuch:

```bash
sudo -u sluice-ner HF_HOME=/opt/sluice/models HF_HUB_DISABLE_XET=1 \
  /opt/sluice/.venv/bin/python -c \
  "from huggingface_hub import snapshot_download; print(snapshot_download('<repo>'))"
```

**Das Modell-Repo allein genügt nicht.** GLiNER lädt den Tokenizer aus dem
**Basis-Encoder-Repo** nach — bei `urchade/gliner_multi_pii-v1` ist das
`microsoft/mdeberta-v3-base`. Wer nur das Modell vorlädt, bekommt beim ersten Start
trotzdem einen Netzzugriff und ohne Netz einen `LocalEntryNotFoundError` mitten im
`TimeoutStartSec`-Fenster. Beide Repos gehören also in den Cache:

```bash
sudo -u sluice-ner HF_HOME=/opt/sluice/models HF_HUB_DISABLE_XET=1 \
  /opt/sluice/.venv/bin/python -c "
from huggingface_hub import snapshot_download
for r in ('<modell-repo>', 'microsoft/mdeberta-v3-base'):
    print(r, '->', snapshot_download(r))"
```

Prüfen lässt sich die Vollständigkeit mit `HF_HUB_OFFLINE=1`: läuft der Dienst damit
hoch, ist der Cache vollständig und die Firewall darf ausgehend wieder zu (§6). Welches
Basis-Repo ein anderes Modell braucht, verrät der erste Ladeversuch — der Repo-Name steht
in der Fehlermeldung bzw. in der Tokenizer-Warnung von `transformers`.

`HF_HUB_DISABLE_XET=1` ist hier kein Detail: HuggingFace lädt große Dateien über eine
eigene Chunk-Infrastruktur (`hf_xet`) mit anderen Endpunkten als der normale
HTTPS-Download. Hinter einer restriktiven Firewall — und der NER-Host steht per Definition
hinter einer — bricht das gern mitten im Transfer ab (`CAS Client Error: … error decoding
response body`). Der klassische Pfad ist langsamer, aber robust und nimmt einen
abgebrochenen Download wieder auf. Hilft das nicht, `pip uninstall hf-xet` — dann gibt es
den Pfad gar nicht mehr; das kostet nur Tempo, nichts an Funktion.

Wie die Provider-Gateways (Rev. 6/8) ist das ein **interner** Dienst: eigener System-User,
eingehend nur vom Sluice-Kern (Firewall), optional Shared Secret `SLUICE_NER_TOKEN`. Er
sieht Rohtext — für ihn gilt dieselbe Netz-Regel wie für den Kern, nicht die lockere eines
Hilfsdienstes.

Das Modell wird **beim Start** geladen, nicht beim ersten Request: ein Dienst, der
`/v1/health` bejaht und dann bei `/v1/detect` scheitert, würde Sluice erst mitten im
Egress-Pfad blockieren. `TimeoutStartSec=300`, weil das Laden dauert.

---

## 9. Offene Punkte

| Punkt | Was fehlt | Stand |
|---|---|---|
| **Schwellwert-Kalibrierung** | Deutschsprachiger Dev/Test-Split aus dem realen Anwendungsfeld; danach `scripts/eval_ner.py` und Ergebnis nach §5. Der ausgelieferte Wert ist ein recall-orientierter Startwert. | offen |
| **Modellauswahl** | Latenz ist gemessen (§3.1), Recall nicht. `knowledgator` ist schneller, ob es auf deutschem Text besser findet als `urchade`, sagt nur die Evaluation. Erster Hinweis auf geringere Precision: Fehlalarm auf einem Emoji (§4.1). | offen — Latenz entschieden, Qualität nicht |
| **Offsets bei mDeBERTa** | Beide Kandidaten liefern korrekte Zeichen-Offsets, auch bei Umlauten, `ß` und außerhalb der BMP (§4.1). Die `transformers`-Warnung ist für die zurückgegebenen Koordinaten folgenlos. Die Prüfung ist **vor jedem Modellwechsel zu wiederholen**. | erledigt 2026-08-11 |
| **`model_revision`** | In `ner.env` und im Profil eintragen. Für `knowledgator/gliner-pii-base-v1.0` ist der Hash bekannt: `61726e0ad791dcab3e29339bbec3ad42ded65641`. | offen |
| **Deployment-Entscheidung CPU/GPU** | Auf der VM gemessen (§3.1): torch-CPU trägt nicht, ONNX fp32 schon für kurze Texte. GPU bleibt die Option für lange Dokumente. | **beantwortet für CPU**, GPU offen |
| **INT8/Quantisierung** | Gemessen und **verworfen**: ohne VNNI ~18 % langsamer als fp32 (§3.1). Erst wieder relevant auf Hardware ab Cascade Lake. | erledigt |
| **Qwen3 → GLiNER** | Der `llama-server` auf dem Zielhost wird durch den NER-Dienst ersetzt. Vorher prüfen, ob Qwen3 dort noch andere Konsumenten hat. | offen, nur bei GPU-Variante |
| **Transport bei entferntem Betrieb** | Tunnel oder TLS wählen (§5a), `SLUICE_NER_TOKEN` setzen, Port 17900 auf die Kern-IP beschränken. | offen, nur bei GPU-Variante |
| **Feinschliff `pii_de`** | KFZ- und Telefonmuster sind recall-orientiert weit gefasst; die False-Positive-Rate auf echten Texten ist noch nicht vermessen. | offen |
| **Blockierender `detect()`** | Behoben 2026-08-11: die Erkennung läuft im Threadpool (Loop bleibt frei, `/v1/health` antwortet unter Last) und bleibt durch ein Lock serialisiert — nebenläufige Läufe auf demselben Modell sind weder für GLiNER belegt noch mit §5.4 vereinbar. | erledigt |
