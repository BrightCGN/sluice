# Evaluationsdatensatz aufbauen — Dev/Test-Split für `pii_ner`

> **Zweck:** Ohne annotierten Datensatz ist der Schwellwert nicht kalibrierbar und die
> Modellwahl nur eine Geschwindigkeitsentscheidung. Dieses Dokument beschreibt, wie der
> Split entsteht — Umfang, Auswahl, Annotationsregeln, Ablauf.
> **Bezug:** `NER-SERVICE.md` §5 (was evaluiert wird), `scripts/eval_ner.py` (Werkzeug),
> Spec §5.4 (warum Recall vor Precision).

---

## 0. Vorab: das ist eine PII-Sammlung

Der Datensatz besteht aus **echten personenbezogenen Daten** — das ist kein Nebeneffekt,
sondern die Anforderung: auf synthetischem Text kalibrierte Schwellwerte übertragen sich
nicht. Damit ist die Datei selbst schutzbedürftig, und zwar mindestens so sehr wie das,
was Sluice sonst zurückhält.

**Nicht ins Repo.** Ein einmal committeter Datensatz ist über den Git-Verlauf praktisch
nicht mehr zu entfernen — auch nach `git rm` liegt er in jedem Klon. Ablage außerhalb des
Checkouts:

```bash
sudo install -d -o sluice-ner -g sluice-ner -m 700 /var/lib/sluice-eval
```

Als Netz darunter ist `docs/eval/` in `.gitignore` eingetragen. Das ersetzt die richtige
Ablage nicht, es fängt nur den Griff daneben.

**Aufbewahrung begrenzen.** Der Datensatz wird für Kalibrierung und Modellvergleich
gebraucht, nicht dauerhaft. Wenn beides steht, gehört er gelöscht oder in eine
verschlüsselte Ablage — mit einer Notiz, warum es ihn gab. Was hier entsteht, ist ein
Korpus, den man sonst nirgends hätte.

---

## 1. Umfang: wie viel ist genug?

Die Größe folgt aus dem Ziel. `eval_ner.py` sucht den höchsten Schwellwert, der ein
**Recall-Ziel auf dem schlechtesten Entitätstyp** hält (Default 0,98). Um 0,98 von 0,95
überhaupt unterscheiden zu können, braucht es genug Positive *je Typ*: bei 100 Spans liegt
der Standardfehler des Recalls bei etwa 1,4 Prozentpunkten — knapp, aber brauchbar. Bei 30
Spans sind es rund 2,5 Punkte, und die Kalibrierung wird zum Münzwurf.

| | Ziel | Minimum |
|---|---|---|
| Gold-Spans **je Label** und Split | ~100 | ~50 |
| Dokumente je Split | 80–120 | ~60 |
| Splits | 2 (dev + test) | 2 — **nicht** verhandelbar |

Bei 500–1.500 Zeichen je Dokument und 3–6 Spans darin kommt man mit rund 100 Dokumenten
je Split hin. Realistischer Aufwand für die Annotation: **3–6 Stunden je Split**, wenn die
Regeln aus §3 vorher feststehen; ohne sie deutlich mehr, weil man zweimal anfängt.

**Zwei Splits sind Pflicht, nicht Kür.** Auf demselben Material zu kalibrieren und zu
berichten überschätzt die Güte systematisch — man misst dann, wie gut der Schwellwert zu
den Daten passt, aus denen er stammt. Aufteilen **nach Dokumenten**, nie nach Sätzen aus
demselben Dokument: sonst steckt derselbe Name in beiden Splits und der Test misst
Auswendiglernen.

---

## 2. Auswahl der Texte

### 2.1 Aus dem echten Egress, nicht aus der Fantasie

Die Texte müssen aus dem stammen, was tatsächlich durch Sluice läuft. Generische
NER-Benchmarks überschätzen den Recall auf Haushalts-, Kanzlei- und Werkstatttexten
regelmäßig — dort stehen Namen in Grußformeln, Adressen ohne Schlüsselwörter und Firmen,
die wie Personen heißen.

Zwei Wege an das Material:

- **Audit-Log mit `SLUICE_AUDIT_LEVEL=full`** für einen begrenzten Zeitraum. Das schreibt
  Vorher/Nachher-Paare mit Nutzdaten — bewusst befristet einschalten, danach zurück auf
  `metadata`. Der Log ist dann selbst eine PII-Sammlung und fällt unter §0.
- **Bestehende Dokumente** aus dem Anwendungsfeld, die den Egress-Texten ähneln.

### 2.2 Nach Profil schichten, nicht mischen

Der Schwellwert steht **pro Profil**. Ein gemischter Split aus Eskalationsmeldungen,
Code-Kommentaren und Playlist-Beschreibungen mittelt genau die Unterschiede weg, wegen
derer es die Trennung gibt. Beginne mit dem **einen** Profil, das `pii_ner` bekommen soll —
und baue später einen zweiten Split, wenn ein zweites Profil dazukommt.

### 2.3 Schwere Fälle bewusst aufnehmen

Ein Split aus lauter klaren Fällen liefert einen Schwellwert, der im Betrieb nicht hält.
Gezielt hineinnehmen:

- Namen, die auch Substantive sind (**Frank**, **Wolf**, **Sommer**, **Braun**)
- Firmen, die wie Personen heißen (**Müller GmbH**, **Schmidt & Partner**)
- Adressen ohne Schlüsselwort (**Am Alten Zoll 3**, **Zur Mühle 12a**)
- nicht-deutsche Namen, Namen mit Bindestrich und Apostroph
- Namen in Grußformeln, Signaturen und Betreffzeilen
- Ortsnamen, die auch Personennamen sind (**Frankfurt** vs. **Anne Frank**)

### 2.4 Negativ-Dokumente nicht vergessen

**20–30 % der Dokumente sollen gar keinen Span enthalten.** Ohne sie lässt sich die
False-Positive-Rate nicht in realistischem Verhältnis messen — und genau die entscheidet,
ob ein niedriger Schwellwert im Betrieb erträglich ist oder alles schwärzt.

---

## 3. Annotationsschema

### 3.1 Nur das annotieren, wofür die NER-Stufe zuständig ist

`pii_ner` ist `pii_regex` **plus** Modell (§1). Die Regex-Stufe deckt bereits ab, was feste
Form hat — IBAN, Steuer-ID, SVNR, KVNR, Kreditkarte, E-Mail, IP, MAC, KFZ-Kennzeichen,
Telefon. **Diese Werte gehören nicht ins Gold.** Stünden sie drin, würde der Recall der
NER-Stufe für etwas gemessen, wofür sie gar nicht zuständig ist, und der Schwellwert
geriete zu niedrig.

Annotiert werden ausschließlich die vier Labels, die der Dienst anfordert:

| Label | Was hinein gehört |
|---|---|
| `person` | Personennamen |
| `organization` | Firmen, Behörden, Vereine |
| `address` | Straße + Hausnummer |
| `location` | Orte, Stadtteile, Länder, PLZ+Ort |

### 3.2 Grenzen exakt festlegen — hier entscheidet sich die Messung

`eval_ner.py` vergleicht **standardmäßig exakt**: `start` *und* `end` müssen stimmen. Ein
Span, der den Namen überdeckt, aber ein Zeichen daneben endet, zählt als Fehler *und* als
Fehlalarm. Uneinheitliche Grenzen im Gold erzeugen deshalb schlechte Zahlen, die dem
Modell angelastet werden, obwohl sie aus der Annotation stammen.

Deshalb: Regeln **vor** der ersten Zeile festlegen und aufschreiben. Vorschlag:

| Fall | Regel | Beispiel |
|---|---|---|
| Anrede / Titel | **nicht** mitannotieren | `Frau Dr. ‹Anna Schmidt›` |
| Rechtsform | **mit**annotieren | `‹Acme GmbH›` |
| Hausnummer | **mit**annotieren | `‹Grüne Straße 7›` |
| PLZ + Ort | eigener `location`-Span | `‹50667 Köln›` |
| Satzzeichen | **nie** mitannotieren | `‹Köln›.` |
| Genitiv-/Beugungsendung | **nicht** mitannotieren | `‹Schmidt›s Auto` |
| Vorname allein | annotieren, wenn er eine Person bezeichnet | `Hallo ‹Anna›` |
| Verschachtelung | **keine** — bei Überlappung der längere sinnvolle Span | `‹Acme GmbH›`, nicht zusätzlich `‹Acme›` |

Diese Tabelle ist ein Vorschlag, keine Wahrheit. Entscheidend ist nicht *welche* Regel,
sondern dass sie **eine** ist und schriftlich vorliegt.

#### Festgelegt für dieses Projekt (2026-08-11)

Verbindlich für jeden Split, der ab hier annotiert wird. Wer eine Regel ändert, ändert die
Messgrundlage — dann ist der alte Split nicht mehr mit dem neuen vergleichbar, und beide
gehören neu ausgewertet. Die Vorschlagstabelle oben gilt unverändert weiter; hier stehen
nur die Fälle, die sie offenlässt.

| Fall | Regel | Warum |
|---|---|---|
| **Interne System-, Projekt- und Hostnamen** (Projektnamen, NAS-Freigaben, Servernamen) | **nicht** annotieren | Zuständigkeit der Regex-/Wörterbuch-Stufe (`infra`-Detektor, `dictionary_terms`), nicht der NER-Stufe (§3.1). Wer sie ins Gold nimmt, misst die falsche Stufe: fehlende Treffer werden dem Modell angelastet, obwohl sie deterministisch besser lösbar sind. |
| **Personen des öffentlichen Lebens** (Künstler, Autoren, Politiker) | annotieren wie jede Person | Eine Regel statt einer Ermessensfrage. „Ist diese Person prominent genug?" ist beim Annotieren nicht reproduzierbar zu beantworten und wäre damit die größte Quelle uneinheitlicher Grenzen. Der Preis ist Übermaskierung in Medientexten — die billigere Fehlerart (§5.4). |
| **Mehrzeilige Postanschrift** | Straße + Hausnummer als `address`, `PLZ Ort` als eigener `location`-Span | Folgt der Vorschlagstabelle. Getrennte Typen erlauben getrennte Diagnose: `eval_ner.py` zielt auf den **schlechtesten** Entitätstyp, und ein schwacher `location`-Recall bliebe in einem Sammel-`address` unsichtbar. |
| **Namen innerhalb von E-Mail-Adressen, Benutzernamen, Dateipfaden** | **nicht** annotieren | Dieselbe Begründung wie bei den Systemnamen: E-Mail, Pfad und Konto haben feste Form und gehören der Regex-Stufe. Der Name *im* Bezeichner wird nicht doppelt gezählt. |
| **Rollen- und Funktionsbezeichnungen** („der Geschäftsführer", „die Kollegin") | **nicht** annotieren | Bezeichnet keine identifizierbare Person. Sonst wandert die Grenze zwischen Annotatoren. |
| **Ortsangabe im Organisationsnamen** („Acme GmbH, Köln") | `‹Acme GmbH›`, `‹Köln›` — zwei Spans | Keine Verschachtelung (Vorschlagstabelle); das Komma trennt zwei eigenständige Angaben. |
| **Bindestrich-Doppelnamen** („Anna Schmidt-Müller") | ein `person`-Span über den ganzen Namen | Der Bindestrich ist Namensbestandteil, keine Grenze. |
| **Hausnummer mit Zusatz** („Grüne Straße 7a", „7–9") | mit annotieren | Gehört zur Anschrift; ein abgeschnittener Zusatz wäre ein Teiltreffer und zählte exakt gewertet doppelt negativ. |

**Zur Textauswahl bei einem neuen Profil:** §2.1 verlangt Texte aus dem *echten* Egress.
Ein frisch angelegtes Profil hat naturgemäß keinen. Nimm deshalb den Verkehr, den es
**übernehmen** wird — die Anfragen der Konsumenten, die künftig darauf zeigen — und nicht
erfundene Sätze. Sonst kalibrierst du auf einer Textsorte, die so nie ankommt.

### 3.3 Format

Eine Zeile je Dokument, UTF-8, JSONL:

```json
{"text": "Am Montag traf Anna Schmidt die Firma Acme GmbH in Köln.", "spans": [{"start": 15, "end": 27, "label": "person"}, {"start": 38, "end": 47, "label": "organization"}, {"start": 51, "end": 55, "label": "location"}]}
```

Offsets sind **Zeichen**-Offsets in Python-Zählung (Codepoints), halboffen `[start, end)`.
Labels klein geschrieben. Ein Dokument ohne Spans bekommt `"spans": []` — nicht weglassen.

### 3.4 Gegenprobe nach dem Annotieren

Jede Zeile maschinell prüfen, bevor evaluiert wird — ein verrutschter Offset im Gold ist
später nicht von einer Modellschwäche zu unterscheiden:

```bash
/opt/sluice/.venv/bin/python - <<'PY'
import json, sys
pfad = "/var/lib/sluice-eval/de-dev.jsonl"
fehler = 0
for nr, zeile in enumerate(open(pfad, encoding="utf-8"), 1):
    d = json.loads(zeile)
    for s in d["spans"]:
        wert = d["text"][s["start"]:s["end"]]
        if not wert.strip() or wert != wert.strip():
            print(f"Zeile {nr}: Grenze unsauber -> {wert!r}"); fehler += 1
        if s["label"] not in ("person","organization","address","location"):
            print(f"Zeile {nr}: unbekanntes Label {s['label']!r}"); fehler += 1
    print(f'Zeile {nr}: {len(d["spans"])} Spans | ' +
          " | ".join(repr(d["text"][s["start"]:s["end"]]) for s in d["spans"]))
print("FEHLER:", fehler)
PY
```

Die ausgeschnittenen Werte einmal durchlesen. Was dort komisch aussieht, ist ein
Annotationsfehler — und jeder davon kostet später doppelt.

---

## 4. Qualität der Annotation prüfen

Bei einer einzelnen annotierenden Person gibt es kein Maß für Konsistenz — und
Inkonsistenz sieht in der Auswertung exakt aus wie ein schlechtes Modell.

**Vorschlag:** 20 Dokumente **zweimal** annotieren, mit mindestens einer Woche Abstand und
ohne die erste Fassung anzusehen. Danach vergleichen. Weichen mehr als etwa 5 % der Spans
ab, ist das Schema unterbestimmt — dann §3.2 nachschärfen und **nicht** die Modelle
vergleichen, bevor das behoben ist. Jede Zahl aus einem inkonsistenten Gold ist Rauschen
mit Nachkommastellen.

---

## 5. Auswerten

```bash
/opt/sluice/.venv/bin/python /opt/sluice/scripts/eval_ner.py \
    --dev  /var/lib/sluice-eval/de-dev.jsonl \
    --test /var/lib/sluice-eval/de-test.jsonl \
    --model urchade/gliner_multi_pii-v1 \
    --model knowledgator/gliner-pii-base-v1.0 \
    --target-recall 0.98 \
    --json /var/lib/sluice-eval/ergebnis.json
```

### 5.1 Der Diagnose-Trick: einmal mit `--relaxed`

Denselben Lauf zusätzlich mit `--relaxed` fahren (Überlappung statt exaktem Match) und die
Zahlen nebeneinanderlegen:

| Beobachtung | Bedeutung |
|---|---|
| beide ähnlich | Die Grenzen sind sauber; die Zahlen bewerten das Modell. |
| `--relaxed` **deutlich** besser | Das Modell *findet* die Entitäten, trifft die Grenzen aber anders als das Gold. Das ist meist ein **Annotationsproblem** (§3.2), keine Modellschwäche. |
| beide schlecht | Das Modell findet die Entitäten wirklich nicht. |

Diese Unterscheidung vor dem Modellvergleich machen — sonst wählt man das Modell, dessen
Grenzkonvention zufällig zur eigenen passt.

### 5.2 Ergebnis eintragen

Der gewählte Schwellwert gehört ins Profil (`[profile.X.ner] threshold`), zusammen mit
`model_repo`, `model_revision` und `model_precision`. Erst damit steht in der
Anonymisierungs-Identität `threshold_calibrated: true` — und erst dann ist die
Recall-Zusage mehr als eine Behauptung.

Die Ergebnistabelle je Modell und Entitätstyp gehört nach `NER-SERVICE.md` §5.

> **Warnt das Skript, dass kein Schwellwert das Ziel hält, ist das ein Ergebnis, kein
> Fehler.** Es wählt dann bewusst nichts. Die richtige Reaktion ist eine Entscheidung —
> Recall-Ziel senken und das begründen, anderes Modell, oder `pii_regex` plus
> `dictionary_terms` für dieses Profil —, nicht ein stillschweigend übernommener F1-Wert.

---

## 6. Reihenfolge

1. Ablage anlegen (§0), Profil auswählen (§2.2). Regeln stehen fest (§3.2, „Festgelegt für
   dieses Projekt") — Schritt 2 kann sie nachschärfen, aber nur schriftlich und für alle
   Splits gemeinsam.
2. 20 Dokumente annotieren, Gegenprobe (§3.4), Regeln nachschärfen. **Erst dann weiter.**
3. Rest annotieren, nach Dokumenten in dev/test teilen.
4. Konsistenz prüfen (§4).
5. Auswerten, exakt und `--relaxed` (§5.1).
6. Schwellwert und Modell ins Profil, Ergebnis in `NER-SERVICE.md` §5.
7. Datensatz aufräumen oder sichern (§0).

Schritt 2 ist der, den man überspringen möchte und nicht überspringen sollte. Zwanzig
Dokumente kosten eine halbe Stunde; ein nachträglich geändertes Schema kostet den ganzen
Split.
