# Sluice — Installation & Betrieb auf der Sluice-VM

> **Zielsystem:** eigene VM, ID **8740**, IP **192.168.87.40**, Port **8000**.
> **Bezug:** `SLUICE-BOUNDARY-SPEC.md` (Spec, Wahrheitsquelle) — §-Verweise unten zeigen dorthin.
> Getestet gegen Debian 12 / Ubuntu 24.04; jede Distribution mit Python ≥ 3.11 funktioniert.

Sluice ist die eine Sanitisierungs-Boundary (§1): **alle** Projekte sprechen diese VM an,
nur diese VM spricht die KI-Provider. Die Installation ist bewusst zustandsarm — der Service
hält v1 keine persistenten Daten (Mappings nur im Speicher, §8), die VM ist jederzeit neu
aufbaubar.

**Zwei Service-Arten, verbindlich getrennt (Rev. 7):** der Kern (`sluice.service`) und
pro Provider ein eigenständiges Gateway (`sluice-gateway@<provider>`). Der Kern ruft
Provider **nie** direkt — ohne laufendes Gateway (und dessen URL in `sluice.env`) ist
jeder Egress zu diesem Provider ein Konfigurationsfehler, fail-closed.

---

## 0. Überblick

```
Konsumenten (Temper, Aider, Crate, …)          Provider (nur von den Gateways erreichbar)
        │  HTTP :8000 (/v1/…)                       ▲  HTTPS :443
        ▼                                           │
┌─────────────────────────── VM 8740 · 192.168.87.40 ───────────────────────────┐
│  systemd: sluice.service (Kern — keine Provider-Keys)                         │
│    └─ uvicorn sluice.server:app  (User: sluice, gehärtet, fail-closed)        │
│  systemd: sluice-gateway@<provider>  (je Provider, Ports ab 17890, §5.1)      │
│    └─ uvicorn sluice.gateway:app  (eigener User sluice-gw-<provider>,         │
│                                    hält NUR den Key seines Providers)         │
│  systemd: sluice-ner.service  (nur für pii_ner, Port 17900, §5.2)             │
│    └─ uvicorn sluice.ner.service:app  (eigener User sluice-ner, kein Egress)  │
│  /opt/sluice          Code + venv                                             │
│  /etc/sluice          profiles.toml (§4) + sluice.env (Gateway-URLs, §7.3)    │
│                       + gateway-<provider>.env (je ein Provider-Key)          │
│                       + ner.env (Modell/Revision, §5.2)                       │
└───────────────────────────────────────────────────────────────────────────────┘
```

| Pfad | Inhalt | Eigentümer / Rechte |
|---|---|---|
| `/opt/sluice` | Repo-Checkout + `.venv` | `sluice:sluice`, read-only im Betrieb |
| `/etc/sluice/profiles.toml` | Konsumenten-Profile (§4) | `root:sluice`, `640` |
| `/etc/sluice/sluice.env` | Gateway-URLs des Kerns (§7.3, Rev. 7 — keine Provider-Keys) | `root:root`, `600` |
| `/etc/systemd/system/sluice.service` | Unit (aus `deploy/`) | `root:root`, `644` |
| `/etc/systemd/system/sluice-gateway@.service` | Template-Unit der Provider-Gateways (§5.1) | `root:root`, `644` |
| `/etc/sluice/gateway-<provider>.env` | Host/Port + Key je Gateway (§5.1) | `root:root`, `600` |
| `/etc/systemd/system/sluice-ner.service` | Unit des NER-Dienstes (§5.2, nur für `pii_ner`) | `root:root`, `644` |
| `/etc/sluice/ner.env` | Modell, Revision, Präzision, Backend (§5.2) | `root:root`, `600` |
| `/opt/sluice/models` | HF-Modell-Cache des NER-Dienstes | `sluice-ner:sluice-ner`, `700` |

### 0.1 Schnellweg: `deploy/bootstrap.sh`

Die Schritte 1–5 gibt es auch als Skript. Es ist idempotent — mehrfaches Ausführen ist
der normale Update-Weg — und **überschreibt bestehende Konfiguration in `/etc/sluice`
nie**: eine ausgefüllte `profiles.toml` oder eine Gateway-Env mit eingetragenem Key
bleibt unangetastet.

```bash
sudo bash deploy/bootstrap.sh                      # Kern + alle vier Gateways
sudo SLUICE_WITH_NER=1 bash deploy/bootstrap.sh    # zusätzlich der NER-Dienst (§5.2)
```

| Variable | Default | Wirkung |
|---|---|---|
| `SLUICE_PROVIDERS` | `anthropic openai gemini mistral` | Welche Gateways auf **dieser** Maschine laufen — beim Umzug eines Gateways dort nur den einen Provider setzen |
| `SLUICE_BIND_HOST` | `192.168.87.40` | Bind-Adresse des Kerns; passt die *installierten* Units/Envs an, die Repo-Dateien bleiben unberührt |
| `SLUICE_WITH_NER` | `0` (aus) | Legt User `sluice-ner`, `models/`, `ner.env` und `sluice-ner.service` an und installiert das Modell-Extra |
| `SLUICE_NER_EXTRA` | `ner` (torch) | Alternativ `ner-onnx` / `ner-onnx-gpu` — **erst nach der Messung** wählen (§5.2) |

Der NER-Dienst ist bewusst Opt-in: er zieht Modell-Gewichte und im torch-Pfad über ein
Gigabyte Abhängigkeiten nach, die eine VM ohne `pii_ner`-Profil nichts angehen.

**Das Skript startet nichts** (§4.3): ohne Profile und Keys wäre das nur ein Service, der
alles blockiert. Die Freigabe bleibt der manuelle, letzte Schritt — es druckt am Ende die
passende Liste. Wer verstehen will, *was* dabei passiert (oder von Hand nachziehen muss),
liest weiter; die folgenden Abschnitte sind das manuelle Äquivalent.

---

## 1. Voraussetzungen

- VM mit Debian 12 / Ubuntu 24.04 (1 vCPU / 1 GiB RAM reichen für den Start; Sluice ist
  I/O-gebunden, nicht CPU-gebunden).
  **Mit NER-Dienst (§5.2) gilt das nicht mehr:** dort liegt ein Modell im Speicher und
  die Erkennung ist CPU-gebunden. Wie viel es konkret braucht, ist eine Messung, keine
  Schätzung — `scripts/probe_ner_hardware.py` vor der Dimensionierung laufen lassen.
  Rechne zusätzlich mit einigen GB Plattenplatz für venv (torch) und Modell-Cache.
- Statische IP `192.168.87.40` konfiguriert.
- Ausgehend HTTPS (443) zu den Provider-APIs erlaubt (Liste in Schritt 6).
- Zugriff auf das Repo `github.com/BrightCGN/sluice` (Deploy-Key oder `scp` vom Arbeitsrechner).

```bash
sudo apt update
sudo apt install -y python3 python3-venv git curl
python3 --version    # muss ≥ 3.11 sein
```

---

## 2. Service-User anlegen

Eigene System-User ohne Login-Shell — kein Service läuft als root, und **jedes Gateway
läuft unter seinem eigenen User** (Rev. 8): User-Isolation zusätzlich zur
Prozess-Isolation — kein Gateway kann Dateien oder Speicher eines anderen (oder des
Kerns) lesen, und beim Umzug auf einen eigenen Server wandert genau ein User mit.

```bash
# Kern:
sudo useradd --system --home /opt/sluice --shell /usr/sbin/nologin sluice
# Ein User pro Gateway (nur die anlegen, deren Gateway auf diesem Server läuft):
for p in anthropic openai gemini mistral; do
  sudo useradd --system --home /opt/sluice --shell /usr/sbin/nologin sluice-gw-$p
done
```

Der NER-Dienst bekommt aus demselben Grund einen eigenen User — mit dem Unterschied, dass
er als einziger Dienst **unsanitisierten Rohtext** sieht (§7.5). Er wird nur gebraucht,
wenn mindestens ein Profil `mode = "pii_ner"` nutzt, und deshalb erst in Schritt 5.2
angelegt.

---

## 3. Code installieren

**Variante A — git clone (empfohlen, macht Updates zum `git pull`):**

```bash
sudo git clone https://github.com/BrightCGN/sluice.git /opt/sluice
```

**Variante B — vom Arbeitsrechner kopieren (falls kein Deploy-Key auf der VM):**

```bash
# auf dem Arbeitsrechner:
rsync -a --exclude .venv --exclude .git ~/git/sluice/ root@192.168.87.40:/opt/sluice/
```

> **`--exclude .venv` ist nicht kosmetisch.** Ein venv ist **nicht verschiebbar**: die
> Konsolen-Skripte in `bin/` (darunter `uvicorn` und `pip`) tragen den absoluten
> Interpreterpfad in ihrer Shebang-Zeile. Mitkopiert zeigen sie weiter auf den
> Arbeitsrechner, und die Dienste scheitern mit `203/EXEC` „Permission denied" — auf das
> *Skript*, dessen Rechte einwandfrei sind, während der *Interpreter* gemeint ist. Das
> venv gehört auf die Zielmaschine gebaut, nie übertragen.

Dann venv anlegen und installieren:

```bash
sudo python3 -m venv /opt/sluice/.venv
sudo /opt/sluice/.venv/bin/pip install -e /opt/sluice
sudo chown -R sluice:sluice /opt/sluice
```

> **Warum `-e` (editierbar)?** Damit es genau **eine** Codequelle gibt. Ohne `-e` liegt
> eine zweite Kopie unter `.venv/lib/python3.*/site-packages/sluice/`, und welche der
> beiden gilt, hängt am Arbeitsverzeichnis: uvicorn stellt mit `--app-dir` (Default `""`)
> das cwd an den Anfang von `sys.path`, und alle drei Units setzen
> `WorkingDirectory=/opt/sluice`. Die Dienste laufen dann aus dem Quellbaum, ein Skript
> mit anderem cwd aber aus der Kopie — die nach einem reinen Datei-Deploy (rsync/tar ohne
> `pip`) veraltet ist, **ohne dass irgendetwas fehlschlägt**. Ein Chokepoint, dessen
> Version vom Arbeitsverzeichnis abhängt, ist keiner. Steigst du auf einer bestehenden
> VM um, muss eine übrig gebliebene `site-packages/sluice/` weg — der Pfad aus dem
> `.pth`-File wird *nach* `site-packages` in `sys.path` eingehängt, die Kopie gewinnt
> also weiterhin. `bootstrap.sh` räumt sie selbst ab und prüft das Ergebnis.

`/opt/sluice` muss für „other" lesbar bleiben (Standard-Umask, o+rX) — die Gateways
laufen unter eigenen Usern (Rev. 8, Schritt 2) und teilen sich nur den Code, sonst nichts.

Kurztest (noch ohne Profile — erwartet ist Default-Deny, kein Fehler). **Nicht aus
`/opt/sluice` heraus ausführen:** dort liegt der Quellbaum ohnehin vorn in `sys.path`, der
Test wird grün und sagt nichts über die Installation. Deshalb `cd /` und den aufgelösten
Pfad mit ausgeben lassen:

```bash
cd / && sudo -u sluice /opt/sluice/.venv/bin/python \
    -c "import sluice.server as m; print('ok —', m.__file__)"
# erwartet: ok — /opt/sluice/sluice/server.py
```

---

## 4. Konfiguration

### 4.1 Profile — `/etc/sluice/profiles.toml` (§4)

**Ohne diese Datei startet Sluice im Default-Deny (§4.3): der Service läuft, blockiert aber
jeden Egress.** Das ist Absicht (fail-closed), keine Störung.

> Vollständige Feld-Referenz inkl. Fallstricken und Fehlerbildern: **`PROFILES.md`**. Hier steht
> nur das Startbeispiel für die Erstinstallation.

Jedes Projekt bekommt **ein** Profil. Startbeispiel:

```toml
# `strict` ist der Auslieferungs-Default (Rev. 9, safety first) — `mode` kann entfallen.
# `strict` redigiert selbst; wo der Konsument bereits generalisiert, `mode = "generalizing"`.
[profile."temper"]
mode                = "generalizing"          # verifiziert den vom Konsumenten generalisierten Text
egress_enabled      = true
allowed_purposes    = ["external_escalation", "promotion_upload"]
provider_allowlist  = ["anthropic", "gemini"]
detector_profile    = "infra"

# Reversibel ist explizites Opt-in (§2) — nur wo die Rückübersetzung gebraucht wird.
[profile."aider-code"]
mode                = "pseudonymizing"
egress_enabled      = true
allowed_purposes    = ["code_completion"]
provider_allowlist  = ["anthropic"]
detector_profile    = "code"
  [profile."aider-code".reversible]
  scope             = "session"
  ttl_seconds       = 3600
  storage           = "memory"

# strict + Profil-Wörterbuch (Rev. 10): auto-redigiert, deckt via dictionary_terms auch
# freie Namen/Adressen ab, die Regex nicht fängt (§5.1.1).
[profile."crate"]
mode                = "strict"
egress_enabled      = true
allowed_purposes    = ["playlist_curation"]
provider_allowlist  = ["anthropic", "gemini"]
detector_profile    = "media"
dictionary_terms    = ["Richard", "Musterstraße 12"]
```

```bash
sudo mkdir -p /etc/sluice
sudo cp /opt/sluice/deploy/sluice.env.example /etc/sluice/sluice.env
sudoedit /etc/sluice/profiles.toml     # Inhalt wie oben, an die eigenen Projekte angepasst
sudo chown root:sluice /etc/sluice/profiles.toml && sudo chmod 640 /etc/sluice/profiles.toml
```

Gültige Provider-Namen für die Allowlist: `anthropic` (Alias: `claude`), `openai`,
`gemini`, `mistral`.

### 4.2 Kern-Konfiguration — `/etc/sluice/sluice.env` (§7.3)

```bash
sudoedit /etc/sluice/sluice.env
sudo chown root:root /etc/sluice/sluice.env && sudo chmod 600 /etc/sluice/sluice.env
```

Der Kern erreicht Provider **ausschließlich über die eigenständigen Gateways** (Rev. 7):
pro genutztem Provider dessen Gateway-URL. Der Kern hält **keine** Provider-Keys — die
liegen nur bei den Gateways (Schritt 5.1). Vorlage: `deploy/sluice.env.example`.

```bash
SLUICE_GATEWAY_ANTHROPIC_URL=http://192.168.87.40:17890
SLUICE_GATEWAY_OPENAI_URL=http://192.168.87.40:17891
SLUICE_GATEWAY_GEMINI_URL=http://192.168.87.40:17892
SLUICE_GATEWAY_MISTRAL_URL=http://192.168.87.40:17893
# optional, muss dann auch in jeder gateway-<provider>.env stehen:
# SLUICE_GATEWAY_TOKEN=…
# Audit-Detailgrad (Rev. 9, §6) — Betreiber-Entscheidung: off | metadata | full.
# Default metadata (loggt DASS, ohne Nutzdaten); full = reviewbares Vorher/Nachher.
# SLUICE_AUDIT_LEVEL=metadata
```

**Modus-Feld (Rev. 9/10):** Profile nutzen `mode` (`strict` Default, §4.1); das alte Feld
`strategy` bleibt als Alias lesbar. `dictionary_terms` (Rev. 10) ergänzt pro Profil freie
Namen/Adressen, die Regex nicht fängt (§5.1.1).

Regeln (§7.3): Keys stehen nur in den `gateway-<provider>.env`-Dateien — **nie** in der
`sluice.env` des Kerns, nie im Profil-TOML, nie im Repo, nie im Audit-Log. Eine fehlende
Gateway-URL ist beim Aufruf des jeweiligen Providers ein harter Fehler (HTTP 500) —
**kein** direkter Provider-Aufruf, kein stiller Fallback. systemd liest die Datei als root
und reicht die Werte in den Prozess; der `sluice`-User selbst kann die Datei nicht lesen.

### 4.3 Shared Secrets erzeugen

Sluice kennt zwei **selbst erzeugte** Geheimnisse. Beide sind Shared Secrets: derselbe
Wert steht auf beiden Seiten, und beide Seiten müssen nach einer Änderung neu starten.

| Secret | Wo | Wozu |
|---|---|---|
| `SLUICE_GATEWAY_TOKEN` | `sluice.env` **und** jeder `gateway-<provider>.env` | Kern ↔ Gateways (§5.1) |
| `SLUICE_NER_TOKEN` | `sluice.env` **und** `ner.env` | Kern ↔ NER-Dienst (§5.2) |

```bash
openssl rand -hex 32
```

32 Byte = 256 Bit. **Hex, nicht Base64:** der Wert geht als HTTP-Header-Wert über die
Leitung und steht in einer Datei, die systemd als `EnvironmentFile` parst — Hex hat
keine Sonderzeichen, die dabei zitiert oder umgedeutet werden könnten, und keine
`=`-Auffüllung, die man beim Kopieren verliert. Führende/abschließende Leerzeichen
werden serverseitig abgeschnitten; ein Zeilenumbruch mitten im Wert nicht.

Direkt in die Dateien schreiben, ohne den Wert je über die Shell-History laufen zu lassen:

```bash
# Ein Token für alle Gateways (der Kern spricht alle mit demselben an):
GW="$(openssl rand -hex 32)"
printf 'SLUICE_GATEWAY_TOKEN=%s\n' "$GW" | sudo tee -a /etc/sluice/sluice.env >/dev/null
for p in anthropic openai gemini mistral; do
  printf 'SLUICE_GATEWAY_TOKEN=%s\n' "$GW" | sudo tee -a /etc/sluice/gateway-$p.env >/dev/null
done
unset GW

# NER-Token (nur mit §5.2; bei entferntem Betrieb Pflicht, nicht optional):
NER="$(openssl rand -hex 32)"
printf 'SLUICE_NER_TOKEN=%s\n' "$NER" | sudo tee -a /etc/sluice/sluice.env >/dev/null
printf 'SLUICE_NER_TOKEN=%s\n' "$NER" | sudo tee -a /etc/sluice/ner.env >/dev/null
unset NER
```

Die auskommentierten `# SLUICE_…_TOKEN=`-Zeilen aus den Vorlagen danach **nicht**
zusätzlich aktivieren: systemd nimmt bei doppelter Zuweisung in einer `EnvironmentFile`
die **letzte**, und eine zweite Zeile im Rücken der ersten ist genau die Art Konfiguration,
die man beim Debuggen übersieht. Kontrolle:

```bash
sudo grep -c '^SLUICE_GATEWAY_TOKEN=' /etc/sluice/sluice.env    # muss 1 sein
```

**Was hier *nicht* erzeugt wird:** die Provider-API-Keys. Die kommen aus den Konsolen von
Anthropic, OpenAI, Google und Mistral und lassen sich nicht lokal generieren.

Beide Tokens sind **optional** und ersetzen die Firewall nicht — sie authentifizieren nur.
Was sie abdecken, ist der Fall, dass jemand *im* erlaubten Netz steht: ohne Token ist jeder
Host, der den Port erreicht, für das Gateway ein gültiger Kern. Für den NER-Dienst wiegt
das schwerer als für die Gateways, weil über ihn **unsanitisierter Rohtext** geht (§5.2).
Verschlüsselung ist es nicht: bei entferntem Betrieb kommt ein Tunnel oder TLS dazu.

---

## 5. systemd-Unit installieren

```bash
sudo cp /opt/sluice/deploy/sluice.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sluice
systemctl status sluice
```

Die Unit bindet an `192.168.87.40:8000`, startet bei Fehlern neu und läuft mit vollem
Sandbox-Härtungsblock (kein root, keine Capabilities, `ProtectSystem=strict`,
Syscall-Filter). Details und Kommentare: `deploy/sluice.service`.

**Nach jeder Änderung an `profiles.toml` oder `sluice.env`:**

```bash
sudo systemctl restart sluice
```

(Profile werden beim Start geladen; einen Hot-Reload gibt es v1 bewusst nicht.)

### 5.1 Provider-Gateways: eigenständige Services, verpflichtend (§7.3, Rev. 7)

Sluice-Kern und Provider-Gateways sind **getrennte Services mit eigenem Lebenszyklus** —
jedes Gateway kann jederzeit auf einen eigenen Server umziehen, ohne dass sich für die
Konsumenten etwas ändert. Der Kern findet ein Gateway ausschließlich über seine URL.
**Die Gateways sind kein Optional:** ohne laufende `sluice-gateway@<provider>`-Instanz
(und deren URL in `sluice.env`) kann der Kern zu diesem Provider keinen Egress ausführen.

```
Konsumenten ──:8000──▶ sluice.service (Kern: Gate → Modus → Verifier → Audit)
                          │ nur nach released=true, via SLUICE_GATEWAY_<P>_URL
                          ├──:17890──▶ sluice-gateway@anthropic ──▶ api.anthropic.com
                          ├──:17891──▶ sluice-gateway@openai    ──▶ api.openai.com
                          ├──:17892──▶ sluice-gateway@gemini    ──▶ generativelanguage…
                          └──:17893──▶ sluice-gateway@mistral   ──▶ api.mistral.ai
```

| Instanz | Port | User (Rev. 8) | Env-Datei | Key darin |
|---|---|---|---|---|
| `sluice-gateway@anthropic` | 17890 | `sluice-gw-anthropic` | `/etc/sluice/gateway-anthropic.env` | `SLUICE_ANTHROPIC_API_KEY` |
| `sluice-gateway@openai` | 17891 | `sluice-gw-openai` | `/etc/sluice/gateway-openai.env` | `SLUICE_OPENAI_API_KEY` |
| `sluice-gateway@gemini` | 17892 | `sluice-gw-gemini` | `/etc/sluice/gateway-gemini.env` | `SLUICE_GEMINI_API_KEY` |
| `sluice-gateway@mistral` | 17893 | `sluice-gw-mistral` | `/etc/sluice/gateway-mistral.env` | `SLUICE_MISTRAL_API_KEY` |

Eigenschaften:

- **User-Isolation (Rev. 8):** jede Instanz läuft unter ihrem eigenen System-User
  `sluice-gw-<provider>` (Schritt 2) — die Template-Unit setzt `User=sluice-gw-%i`.
  `/opt/sluice` bleibt dafür wie vom Checkout world-readable (o+rX); die Env-Dateien
  liest systemd als root, kein Service-User kann fremde Keys lesen.
- **Die Boundary bleibt im Kern.** Ein Gateway wird nur vom Sluice-Dispatch aufgerufen,
  *nachdem* der Verifier released hat — es sieht nie Rohtext. Deshalb: Gateways sind
  **interne Dienste**, eingehend nur vom Sluice-Kern erreichbar (Firewall), **nie** direkt
  von Konsumenten. Optional zusätzlich Shared Secret `SLUICE_GATEWAY_TOKEN` auf beiden
  Seiten — erzeugen mit `openssl rand -hex 32`, Ablauf in §4.3.
- **Key-Isolation:** jedes Gateway hält nur den Key seines Providers; der Kern braucht
  bei dieser Variante **gar keine** Provider-Keys mehr.
- **Umzugsfähig:** zieht ein Gateway auf einen eigenen Server, ändern sich nur
  `SLUICE_HOST` in dessen `gateway-<provider>.env` und die `SLUICE_GATEWAY_<P>_URL`
  in der `sluice.env` des Kerns — sonst nichts.

**Installation der Gateways** (zunächst auf derselben VM 8740; User aus Schritt 2):

```bash
sudo cp /opt/sluice/deploy/sluice-gateway@.service /etc/systemd/system/
for p in anthropic openai gemini mistral; do
  id sluice-gw-$p >/dev/null    # User muss existieren (Schritt 2)
  sudo cp /opt/sluice/deploy/gateway-$p.env.example /etc/sluice/gateway-$p.env
  sudoedit /etc/sluice/gateway-$p.env    # Key eintragen (Host/Port sind vorbelegt)
  sudo chown root:root /etc/sluice/gateway-$p.env && sudo chmod 600 /etc/sluice/gateway-$p.env
done
sudo systemctl daemon-reload
sudo systemctl enable --now sluice-gateway@anthropic sluice-gateway@openai \
                            sluice-gateway@gemini sluice-gateway@mistral
```

**Kern auf die Gateways zeigen lassen** — in `/etc/sluice/sluice.env` (statt der Keys):

```bash
SLUICE_GATEWAY_ANTHROPIC_URL=http://192.168.87.40:17890
SLUICE_GATEWAY_OPENAI_URL=http://192.168.87.40:17891
SLUICE_GATEWAY_GEMINI_URL=http://192.168.87.40:17892
SLUICE_GATEWAY_MISTRAL_URL=http://192.168.87.40:17893
```

Danach `sudo systemctl restart sluice`. Prüfen:

```bash
curl -s http://192.168.87.40:17890/v1/health
# → {"status":"ok","provider":"anthropic"}
```

Nur die Gateways aktivieren, deren Provider in mindestens einer Allowlist stehen.
Ein Provider ohne Gateway-URL ist für den Kern nicht erreichbar — der Aufruf endet
fail-closed mit HTTP 500 `sluice_provider_config` (Rev. 7), nie in einem direkten Call.
Betrieb/Logs je Gateway: `journalctl -u sluice-gateway@anthropic -f`.

**Umzug eines Gateways auf einen eigenen Server:** Schritte 1–3 dieses Dokuments auf dem
neuen Server wiederholen — als User genügt dort der **eine** `sluice-gw-<provider>`
(weder `sluice` noch die anderen Gateway-User; Profile braucht ein Gateway nicht) —,
nur die eine `gateway-<provider>.env` mit angepasstem `SLUICE_HOST` anlegen, Gateway starten,
im Kern die `SLUICE_GATEWAY_<P>_URL` umstellen, `systemctl restart sluice`. Firewall:
Gateway-Port eingehend nur von der Sluice-Kern-IP; ausgehend 443 nur zum eigenen
Provider-Host (Tabelle in Schritt 6).

### 5.2 NER-Dienst: eigenständiger Service (§7.5, Rev. 12)

**Nur nötig, wenn mindestens ein Profil `mode = "pii_ner"` nutzt.** Ohne `pii_ner`-Profil
diesen Schritt überspringen — `pii_regex`, `strict` und die übrigen Modi brauchen ihn nicht.

Der Dienst tut genau eins: Text rein, Spans raus. Er enthält **keine** Anonymisierungslogik
(keine Pseudonym-Zuordnung, keinen Modus-Schalter, keine Profilbindung, keine Maskierung) —
das bleibt im Kern. Erst diese Enge macht das Modell austauschbar und erlaubt, zwei Modelle
vergleichend zu betreiben, **ohne den Chokepoint zu duplizieren**.

**Skriptweg:** `sudo SLUICE_WITH_NER=1 bash deploy/bootstrap.sh` erledigt alles bis
einschließlich `daemon-reload` — nur das Ausfüllen von `ner.env` und das Starten bleiben
manuell. Von Hand ist es das hier:

```bash
sudo useradd --system --home /opt/sluice --shell /usr/sbin/nologin sluice-ner
# Der Cache muss NACH einem rekursiven chmod auf /opt/sluice angelegt werden (Schritt 3),
# sonst räumt das a+rX die 700 wieder weg.
sudo install -d -o sluice-ner -g sluice-ner -m 700 /opt/sluice/models

# Modell-Abhängigkeiten. Sie landen zwangsläufig im selben venv (ein Interpreter für alle
# Units) — der Kern *importiert* sie trotzdem nicht: gliner/torch werden erst in
# sluice.ner.engine lazy geladen, und die läuft nur im NER-Prozess.
# Auch hier -e: ohne das legt pip eine nicht-editierbare Kopie über die editierbare
# Installation und führt die Zweideutigkeit aus Schritt 3 wieder ein.
sudo /opt/sluice/.venv/bin/pip install -e '/opt/sluice[ner]'   # bzw. [ner-onnx], [ner-onnx-gpu]
sudo chown -R sluice:sluice /opt/sluice/.venv && sudo chmod -R a+rX /opt/sluice/.venv

sudo install -m600 -o root -g root /opt/sluice/deploy/ner.env.example /etc/sluice/ner.env
sudoedit /etc/sluice/ner.env       # SLUICE_NER_MODEL + SLUICE_NER_REVISION eintragen!
sudo install -m644 /opt/sluice/deploy/sluice-ner.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now sluice-ner

curl http://127.0.0.1:17900/v1/health   # {"status":"ok","model":"…"}
curl http://127.0.0.1:17900/v1/info     # Modellidentität für die Profilverankerung
```

Zusätzlich muss der **Kern** den Dienst finden — sonst blockiert jedes `pii_ner`-Profil
fail-closed (§5.3). Entweder global in `/etc/sluice/sluice.env`:

```bash
SLUICE_NER_URL=http://127.0.0.1:17900
```

…oder pro Profil unter `[profile."…".ner] url`. Fehlt beides, ist das kein stiller
Fallback auf `pii_regex`, sondern ein Block. Danach `sudo systemctl restart sluice`.

Im Profil verankern (`[profile."…".ner]`, siehe `docs/PROFILES.md` §6a) — `url`,
`threshold`, `model_repo`, `model_revision`, `model_precision`. Zwei Prüfungen laufen
dann beim ersten Kontakt mit dem Dienst, beide fail-closed (§5.4):

- **Identität.** Jeder im Profil *deklarierte* Wert muss dem entsprechen, was `/v1/info`
  meldet — sonst `NerIdentityError`. Ein Profil, das nichts deklariert, verankert auch
  nichts und bekommt die gemeldete Identität nur zu sehen; geprüft wird nur, was dasteht.
  Deshalb ist die Verankerung nach jedem Modell- oder Revisionswechsel nachzuziehen.
- **Schwellwert.** `SLUICE_NER_SCORE_FLOOR` des Dienstes muss **unter** dem
  Profil-`threshold` liegen. Filtert der Dienst schärfer vor als das Profil filtert, wäre
  die verankerte Schwelle wirkungslos — Sluice blockiert, statt eine Recall-Zusage ohne
  Deckung zu tragen.

> **Messstand vom 2026-08-11** (Details und Zahlen: `docs/NER-SERVICE.md` §3.1):
> - **PyTorch auf der VM-CPU trägt nicht** — 1,24 s bei 200 Zeichen, 8,6 s bei 4.000, pro
>   Request im synchronen Egress-Pfad. Zweimal bestätigt; Host-Tuning ändert nichts, weil
>   die Erkennung auf einem festgenagelten Thread läuft.
> - **ONNX Runtime schon** — 173 ms bei 200 Zeichen. Damit ist `pii_ner` für kurze Texte
>   vertretbar, für lange Dokumente nicht. Die Wahl trifft man **pro Profil**.
> - **Quantisierung (uint8) ist hier der falsche Weg** — rund 18 % *langsamer* als fp32,
>   weil die INT8-Gewinne VNNI-Befehle brauchen (erst ab Cascade Lake). Also
>   `onnx/model.onnx`, nicht `onnx/model_quint8.onnx`.
>
> **Zwei Dinge bleiben offen**, beide in `docs/NER-SERVICE.md` §9:
> 1. **Modellwahl.** Die Latenz ist gemessen, der *Recall* nicht. Welches Modell auf
>    deutschem Text besser findet, sagt `scripts/eval_ner.py` auf einem Dev-Split.
> 2. **Schwellwert kalibrieren.** Der ausgelieferte Wert ist ein recall-orientierter
>    Startwert, **keine Kalibrierung** — ohne Dev-Split ist die Recall-Zusage unbelegt.

**Betriebsverhalten, das man kennen muss:** Fällt der NER-Dienst aus oder reißt das
Timeout-Budget, wird **jeder Request eines `pii_ner`-Profils blockiert** — HTTP 503,
`sluice_mode_unavailable`. Das ist Absicht: kein stiller Rückfall auf `pii_regex`, keine
Degradation. Ein Chokepoint, der bei Ausfall durchlässiger wird, ist kein Chokepoint.
Entsprechend überwachen: `journalctl -u sluice-ner -f` und der Kern-Log-Eintrag
`guard.mode_unavailable`.

Der Dienst startet erst, wenn das Modell geladen ist (`TimeoutStartSec=300`) — ein Dienst,
der `/v1/health` bejaht und erst bei `/v1/detect` scheitert, würde den Kern mitten im
Egress-Pfad blockieren.

---

## 6. Firewall — Sluice als einziger Egress-Pfad

Netzwerkseitig wird der technische Egress-Riegel (§1) erst rund: **nur** diese VM darf zu den
Provider-APIs hinaus, und hinein darf nur der Perimeter.

Eingehend:
- TCP `8000` **nur** aus dem internen Netz der Konsumenten (z. B. `192.168.87.0/24`).
- NER-Port `17900` **nur** von der IP des Sluice-Kerns — der Dienst sieht **Rohtext**,
  für ihn gilt dieselbe Netz-Regel wie für den Kern, nicht die lockere eines Hilfsdienstes.
  Ausgehend braucht er **nichts** (kein Egress); nur einmalig 443 zum Modell-Download, das
  danach wieder zu schließen ist.
- Gateway-Ports `17890–17893` **nur** von der IP des Sluice-Kerns (solange beide auf
  derselben VM laufen, reicht localhost/VM-intern) — Konsumenten sprechen **nie** direkt
  mit einem Gateway (§5.1).
- SSH nach eigenem Admin-Standard.


**Auf dem NER-Host (falls der Dienst nicht auf der Kern-VM läuft, §5.2):**
- TCP `17900` **nur** von der IP des Sluice-Kerns. Über diesen Port geht **unsanitisierter
  Rohtext** — dieser Hop trägt mehr Personenbezug als jeder Provider-Aufruf. Zusätzlich
  `SLUICE_NER_TOKEN` setzen (`openssl rand -hex 32`, §4.3) und den Transport
  tunneln/verschlüsseln (WireGuard oder TLS); Sluice blockiert Klartext-HTTP zu einem
  entfernten Host fail-closed, solange `SLUICE_NER_ALLOW_PLAINTEXT_REMOTE` nicht gesetzt
  ist. Das Token ist Authentifizierung, **kein** Ersatz für die Verschlüsselung.

Ausgehend (Ziel-Hosts der Adapter, jeweils TCP 443):

| Provider | Host |
|---|---|
| Anthropic | `api.anthropic.com` |
| OpenAI | `api.openai.com` |
| Google Gemini | `generativelanguage.googleapis.com` |
| Mistral | `api.mistral.ai` |

Beispiel mit ufw:

```bash
sudo ufw default deny incoming
sudo ufw allow from 192.168.87.0/24 to any port 8000 proto tcp
sudo ufw allow from <admin-netz> to any port 22 proto tcp
sudo ufw enable
```

Auf dem **Hypervisor/Router** zusätzlich: ausgehendes 443 zu den vier Hosts nur für
`192.168.87.40` erlauben, für alle anderen internen Maschinen sperren — dann ist Sluice
auch technisch der einzige Weg nach draußen.

---

## 7. Verifikation (Abnahme-Checkliste)

Alle Aufrufe von einem Konsumenten-Host aus (`192.168.87.x`):

**7.1 Health:**

```bash
curl -s http://192.168.87.40:8000/v1/health
# → {"status":"ok","profiles":2,"provider_lock":null}
#   Zahl = geladene Profile; 0 heißt: profiles.toml prüfen!
#   provider_lock: null bei der Sammel-Instanz, Provider-Name bei Gateway-Instanzen (§5.1)
```

**7.2 Guard-Endpoint (§7.1) — sauberer Text geht durch:**

```bash
curl -s -X POST http://192.168.87.40:8000/v1/egress/guard \
  -H 'content-type: application/json' \
  -d '{"profile":"temper","purpose":"external_escalation",
       "raw_text":"db-prod-3 OOM","generalized_text":"Ein Server meldet Speicherdruck"}'
# → {"released":true,"sanitized_text":"Ein Server meldet Speicherdruck",…}
```

**7.3 Verifier blockt (der Riegel funktioniert):**

```bash
curl -s -X POST http://192.168.87.40:8000/v1/egress/guard \
  -H 'content-type: application/json' \
  -d '{"profile":"temper","purpose":"external_escalation",
       "raw_text":"x","generalized_text":"Host 10.0.0.5 down"}'
# → {"released":false,…"reason":"Verifier blockiert: IP-Adresse: 10.0.0.5"}
```

**7.4 Default-Deny (kein Profil → 403):**

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  http://192.168.87.40:8000/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"hi"}],"model":"claude-sonnet-5"}'
# → 403
```

**7.5 Voll-Proxy mit echtem Provider (§7.2, benötigt laufendes Gateway mit gültigem Key):**

```bash
curl -s -X POST http://192.168.87.40:8000/v1/chat/completions \
  -H 'content-type: application/json' \
  -H 'X-Sluice-Profile: aider-code' \
  -H 'X-Sluice-Scope: abnahme-test-1' \
  -d '{"messages":[{"role":"user","content":"Antworte mit genau einem Wort: Bereit?"}],
       "model":"claude-sonnet-5"}'
# → {"object":"chat.completion",…"content":"Bereit"…}
```

**7.6 NER-Dienst (nur wenn §5.2 installiert wurde):**

Zuerst der Dienst selbst, direkt auf der VM — er ist von außen nicht erreichbar:

```bash
curl -s http://127.0.0.1:17900/v1/info
# → {"model":"…","revision":"…","precision":"fp32","backend":"gliner-torch/cpu",…}
#   revision leer? Dann ist die Anonymisierungs-Identität (§5.4) nicht belastbar.
```

Dann der Weg durch den Kern, mit einem `pii_ner`-Profil:

```bash
curl -s -X POST http://192.168.87.40:8000/v1/egress/guard \
  -H 'content-type: application/json' \
  -d '{"profile":"<pii-ner-profil>","purpose":"<erlaubter-purpose>",
       "raw_text":"Bitte an Anna Schmidt, Musterweg 3, weiterleiten.",
       "generalized_text":"Bitte an Anna Schmidt, Musterweg 3, weiterleiten."}'
# → released=true, im sanitized_text stehen Platzhalter statt Name und Adresse
```

**Die wichtigere Probe ist der Ausfall** — sie prüft die Zusage, dass nicht degradiert
wird. NER-Dienst stoppen, denselben Request wiederholen:

```bash
sudo systemctl stop sluice-ner
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  http://192.168.87.40:8000/v1/egress/guard -H 'content-type: application/json' \
  -d '{"profile":"<pii-ner-profil>","purpose":"<erlaubter-purpose>",
       "raw_text":"x","generalized_text":"x"}'
# → 503   (sluice_mode_unavailable — NICHT 200 mit Regex-Ergebnis)
sudo systemctl start sluice-ner
```

Kommt hier **200**, ist irgendwo doch ein Fallback eingebaut — das wäre ein Chokepoint,
der bei Ausfall durchlässiger wird, und damit keiner. Nicht in Betrieb nehmen.

Erst wenn 7.1–7.5 (und ggf. 7.6) wie beschrieben antworten, Konsumenten auf die VM
zeigen lassen.

---

## 8. Betrieb

**Logs** (strukturiert; jede Egress-Entscheidung erscheint mit Profil, Purpose, Modus,
released/blocked und Verifier-Findings):

```bash
journalctl -u sluice -f
journalctl -u sluice --since today | grep "released=False"   # heutige Blocks
journalctl -u sluice-ner -f                                  # nur mit §5.2
```

Bei laufendem NER-Dienst zusätzlich auf `guard.mode_unavailable` im Kern-Log achten: das
ist der Eintrag, mit dem ein `pii_ner`-Profil blockiert, weil der Dienst nicht antwortet.
Er gehört auf dieselbe Aufmerksamkeitsstufe wie ein Gateway-Ausfall — anders als dort
fällt er aber nicht auf, weil der Konsument nur ein 503 sieht.

**Update einspielen:**

```bash
cd /opt/sluice
sudo -u sluice git pull                       # bzw. rsync wie in Schritt 3B
sudo systemctl restart sluice
curl -s http://192.168.87.40:8000/v1/health   # Abnahme 7.1 wiederholen
```

Durch die editierbare Installation (Schritt 3) ist der neue Code mit dem Kopieren bereits
da — ein `pip`-Lauf ist für reine Code-Änderungen **nicht** nötig, der Restart genügt.
Nötig bleibt er, wenn sich `pyproject.toml` ändert: neue Abhängigkeiten, neue Extras, neue
Konsolen-Skripte. Im Zweifel `sudo bash deploy/bootstrap.sh` — der Lauf ist idempotent.

Alternativ und äquivalent: `sudo bash deploy/bootstrap.sh` aus dem aktualisierten
Checkout (idempotent, fasst `/etc/sluice` nicht an — §0.1). Läuft der NER-Dienst mit,
dann `SLUICE_WITH_NER=1` mitgeben, damit auch dessen Extra nachgezogen wird, und ihn
mitneustarten:

```bash
sudo /opt/sluice/.venv/bin/pip install -e '/opt/sluice[ner]'
# Einzeln und in dieser Reihenfolge: bei `restart a b` garantiert systemd keine
# Reihenfolge, und der Kern blockiert jedes pii_ner-Profil, solange der Dienst weg ist.
sudo systemctl restart sluice-ner
sudo systemctl restart sluice
```

Ein Modell- oder Revisionswechsel ist **kein** reines Update: die Verankerung in den
Profilen (`model_repo`/`model_revision`/`model_precision`) muss mitgezogen werden, sonst
blockiert die Identitätsprüfung (§5.2) — absichtlich.

**Backup:** Nur `/etc/sluice/` sichern (Profile + Keys + `ner.env`). `/opt/sluice` ist aus
dem Repo reproduzierbar, der Modell-Cache unter `/opt/sluice/models` aus dem Netz; der
Service selbst hält keinen persistenten Zustand (Mappings sind in-memory mit TTL, §8 —
ein Neustart verwirft sie absichtlich).

**Neuen Konsumenten anschließen:** Profil in `profiles.toml` ergänzen (braucht er einen
neuen Provider: Gateway-Instanz aktivieren + URL in `sluice.env`, §5.1),
`systemctl restart sluice`, Abnahme 7.2/7.4 fürs neue Profil wiederholen.

---

## 9. Troubleshooting

| Symptom | Ursache | Abhilfe |
|---|---|---|
| `/v1/health` zeigt `"profiles":0` | `SLUICE_PROFILES` zeigt ins Leere / TOML-Fehler | `journalctl -u sluice` — Ladefehler brechen den Start mit Ursache ab; Pfad + TOML prüfen |
| Alles antwortet 403 „Default-Deny" | `X-Sluice-Profile`-Header fehlt oder Profilname unbekannt | Header setzen; Name muss exakt dem TOML-Key entsprechen |
| 403 „Purpose … nicht erlaubt" | `purpose` fehlt im Profil | `allowed_purposes` ergänzen oder korrektes `purpose`-Feld senden |
| 403 „Provider … nicht in der Allowlist" | Request-`provider` nicht freigegeben (§4.1) | Allowlist erweitern — bewusste Entscheidung, Provider divergieren in Retention/Training |
| 403 „Verifier blockiert: …" | Roher Identifier im Egress-Kandidaten | **Kein Sluice-Fehler — der Riegel arbeitet.** Konsument muss besser generalisieren, oder reversiblen Modus (Opt-in) nutzen |
| 500 `sluice_provider_config` | `SLUICE_GATEWAY_<P>_URL` fehlt (Rev. 7) / Provider-Name unbekannt / Key im Gateway fehlt | `sluice.env` (URLs) bzw. `gateway-<p>.env` (Key) prüfen, betroffenen Service neu starten |
| 502 `sluice_provider_upstream` | Provider-API down oder Key ungültig | `journalctl` zeigt den HTTP-Status des Providers; Key/Status-Seite des Providers prüfen |
| 502 mit `gateway …` in der reason | Gateway-Service down / URL falsch / Token-Mismatch | `systemctl status sluice-gateway@<p>`; `SLUICE_GATEWAY_<P>_URL` und `SLUICE_GATEWAY_TOKEN` auf beiden Seiten prüfen |
| `start-limit-hit`, „Start request repeated too quickly" | **Folge, nicht Ursache** — systemd hat nach wiederholtem Absturz aufgegeben und startet den Dienst nicht mehr, auch nach behobener Ursache nicht | Erst die echte Fehlermeldung suchen: `journalctl -u <unit> --since -1h \| grep -v '^░░'` — sie steht *vor* dem ersten `start-limit-hit`. Nach dem Fix `sudo systemctl reset-failed <unit>`, sonst bleibt jeder `restart` wirkungslos |
| Gateways scheitern, Kern läuft (oder umgekehrt) | Die Gateway-User sind weder Eigentümer noch Gruppe von `/opt/sluice` — sie kommen nur über `o+rX` an venv und Code. Ein zu enges `chmod` trifft sie zuerst | `sudo -u sluice-gw-anthropic /opt/sluice/.venv/bin/uvicorn --version`. Reparatur wie in der `203/EXEC`-Zeile unten |
| `203/EXEC`, aber `ls -l` am Skript ist einwandfrei und `/opt` hat kein `noexec` | **Das venv stammt von einer anderen Maschine.** Ein venv ist nicht verschiebbar: die Skripte in `bin/` tragen den absoluten Interpreterpfad im Shebang. Zeigt der ins Home des Entwicklers (`0700`), wird aus dem fälligen `ENOENT` ein `EACCES` — die Meldung nennt das Skript, gemeint ist der Interpreter | `head -1 /opt/sluice/.venv/bin/uvicorn` muss `#!/opt/sluice/.venv/bin/python` sein. Sonst neu bauen: `rm -rf /opt/sluice/.venv`, dann `bootstrap.sh` (erkennt das seit dem Fix selbst und baut neu) oder `python3 -m venv` + `pip install -e /opt/sluice` von Hand. **Nie ein venv rsyncen** — nur den Code, das venv gehört auf die Zielmaschine |
| Neuer Code ist kopiert, der Dienst verhält sich aber wie vorher — oder umgekehrt: ein Skript sieht alten Code, während der Dienst neuen fährt | **Zwei Kopien im Spiel.** Eine nicht-editierbare Installation liegt zusätzlich unter `.venv/lib/python3.*/site-packages/sluice/`; welche gilt, hängt am Arbeitsverzeichnis (uvicorn stellt das cwd an den Anfang von `sys.path`, die Units setzen `WorkingDirectory=/opt/sluice`). Nichts schlägt fehl, es gilt nur je nach Aufruf anderer Code | Aufgelösten Pfad **aus `/` heraus** prüfen, nie aus `/opt/sluice` — dort verdeckt der Quellbaum jeden Befund: `cd / && sudo -u sluice /opt/sluice/.venv/bin/python -c "import sluice.policy as m; print(m.__file__)"`. Erwartet ist `/opt/sluice/sluice/policy.py`. Zeigt er nach `site-packages`, ist eine Alt-Installation übrig: `sudo bash deploy/bootstrap.sh` räumt sie ab und prüft nach |
| NER-Dienst lädt kein Modell mehr nach, `ls -ld /opt/sluice/models` zeigt `sluice` statt `sluice-ner` (oder `drwxr-xr-x` statt `700`) | Ein rekursives `chown`/`chmod` über `/opt/sluice` hat die Ausnahme für den Modell-Cache überschrieben — von Hand (Schritt 3) oder durch einen Bootstrap **ohne** `SLUICE_WITH_NER=1` (bis zum Fix war die Korrektur daran gekoppelt, das `chown -R` davor nie). Der NER-User verliert den **Schreib**zugriff auf seinen eigenen Cache; lesen kann er weiter, deshalb fällt es erst beim nächsten Download auf | `sudo chown -R sluice-ner:sluice-ner /opt/sluice/models && sudo chmod -R go-rwx /opt/sluice/models`. Bei Updates auf einer Maschine mit NER-Dienst grundsätzlich `SLUICE_WITH_NER=1` mitgeben — dann laufen auch dessen Prüfungen mit |
| Dienste laufen, sterben aber **nach dem nächsten Reboot** | Ein kaputtes `/opt/sluice` fällt im Betrieb nicht auf — die laufenden Prozesse halten ihre Dateien offen. Erst der Neustart deckt es auf, dann aber bei allen Units gleichzeitig | Nach jedem Eingriff an `/opt/sluice` (bootstrap, `pip`, `chmod`) einmal `sudo systemctl restart sluice 'sluice-gateway@*'` statt bis zum nächsten Reboot zu warten |
| 401 `gateway_unauthorized` bzw. 401 vom NER-Dienst | Token nur auf **einer** Seite gesetzt, Wert abweichend, oder doppelt zugewiesen | Beide Dateien vergleichen (§4.3); `grep -c '^SLUICE_…_TOKEN=' <datei>` muss je `1` ergeben. Nach jeder Änderung **beide** Seiten neu starten — der Wert wird beim Start gelesen |
| Service startet nicht | Python < 3.11, venv kaputt, Port belegt | `journalctl -u sluice -n 50`; `ss -tlnp \| grep 8000` |
| `203/EXEC … Permission denied` auf `.venv/bin/uvicorn` | Der Service-User darf die Datei nicht ausführen — meist, weil ein `pip`-Lauf als root sie mit enger `umask` neu geschrieben hat (Konsolen-Skripte werden bei jeder (Neu-)Installation erzeugt) | **Zuerst `head -1 /opt/sluice/.venv/bin/uvicorn`** — zeigt der Shebang irgendwo anders hin als `/opt/sluice/.venv/bin/python`, ist das venv von einer anderen Maschine (siehe die Zeile darunter). Sonst: `sudo -u sluice /opt/sluice/.venv/bin/uvicorn --version` als echter `exec`-Versuch, nicht `test -x` — das prüft nur die Bits des Skripts und meldet „ok", während `execve` am Interpreter oder an einem `noexec`-Mount scheitert. Ergänzend `findmnt -no OPTIONS -T /opt/sluice` und `namei -l` auf den Shebang-Pfad. Reparatur: `sudo chown -R sluice:sluice /opt/sluice && sudo chmod -R a+rX /opt/sluice` — mit NER-Dienst danach `sudo chown -R sluice-ner:sluice-ner /opt/sluice/models && sudo chmod -R go-rwx /opt/sluice/models`. `bootstrap.sh` zieht das seit dem Fix nach jedem `pip`-Lauf selbst gerade |
| 503 `sluice_mode_unavailable` | NER-Dienst down, Timeout gerissen, oder `SLUICE_NER_URL`/`[profile.…ner] url` fehlt (§5.2) | `systemctl status sluice-ner`, Kern-Log `guard.mode_unavailable`. **Kein Sluice-Fehler im engeren Sinn — so ist es gedacht:** kein stiller Rückfall auf `pii_regex` |
| 503, obwohl `sluice-ner` läuft | Modellidentität weicht ab (`NerIdentityError`) oder `SLUICE_NER_SCORE_FLOOR` > Profil-`threshold` | Reason lesen — sie nennt Feld, verankerten und gemeldeten Wert. `curl 127.0.0.1:17900/v1/info` gegen den `[profile.….ner]`-Block halten |
| 503 mit `ner_text_truncated` bzw. Hinweis auf das Kontextfenster | `max_chars_per_chunk` passt nicht in das Token-Fenster des Modells — das Modell würde still kürzen und der hintere Teil bliebe ungeprüft | `max_chars_per_chunk` im Profil senken (Default 700 passt in 384 Token). **Nicht** durch Anheben des Fensters „lösen": die Blockade ist die Zusage, nicht der Fehler |
| `sluice-ner` startet nicht, Meldung „Präzision widersprüchlich" | `SLUICE_NER_PRECISION` passt nicht zu `SLUICE_NER_ONNX_FILE` | Beide angleichen: `onnx/model.onnx` ⇒ `fp32`, `onnx/model_quint8.onnx` ⇒ `uint8`. Die Präzision geht in die Anonymisierungs-Identität ein und muss der geladenen Datei entsprechen |
| `sluice-ner` startet nicht | `SLUICE_NER_MODEL` leer, Modell-Download fehlgeschlagen, `gliner` fehlt | `journalctl -u sluice-ner -n 50`. Der Dienst lädt das Modell **beim Start** (fail-closed) — er kommt bewusst gar nicht erst hoch, statt später im Egress-Pfad zu scheitern |
| `sluice-ner` bricht mit `TimeoutStartSec` ab | Erststart lädt die Gewichte aus dem Netz | Einmalig 443 für den NER-Host öffnen (§6), Dienst starten, danach wieder schließen — der Cache unter `/opt/sluice/models` bleibt |
| `pii_ner` blockt viel mehr als erwartet | `threshold` ist ein recall-orientierter Startwert, **keine Kalibrierung** | `scripts/eval_ner.py` mit deutschsprachigem Dev-Split fahren, dann den Profil-`threshold` setzen (`docs/NER-SERVICE.md`) |

**Grundsatz bei jeder Störung:** Sluice ist fail-closed. Jeder Fehlerpfad blockiert, statt
Rohtext durchzulassen — eine „hängende" Integration ist immer ein Konfigurations- oder
Erreichbarkeitsproblem, nie ein stiller Datenabfluss.
