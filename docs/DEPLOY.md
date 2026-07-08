# Sluice — Installation & Betrieb auf der Sluice-VM

> **Zielsystem:** eigene VM, ID **8740**, IP **192.168.87.40**, Port **8000**.
> **Bezug:** `SLUICE-BOUNDARY-SPEC.md` (Spec, Wahrheitsquelle) — §-Verweise unten zeigen dorthin.
> Getestet gegen Debian 12 / Ubuntu 24.04; jede Distribution mit Python ≥ 3.11 funktioniert.

Sluice ist die eine Sanitisierungs-Boundary (§1): **alle** Projekte sprechen diese VM an,
nur diese VM spricht die KI-Provider. Die Installation ist bewusst zustandsarm — der Service
hält v1 keine persistenten Daten (Mappings nur im Speicher, §8), die VM ist jederzeit neu
aufbaubar.

---

## 0. Überblick

```
Konsumenten (Temper, Aider, Crate, …)          Provider (nur von dieser VM erreichbar)
        │  HTTP :8000 (/v1/…)                       ▲  HTTPS :443
        ▼                                           │
┌─────────────────────────── VM 8740 · 192.168.87.40 ───────────────────────────┐
│  systemd: sluice.service                                                      │
│    └─ uvicorn sluice.server:app  (User: sluice, gehärtet, fail-closed)        │
│  /opt/sluice          Code + venv                                             │
│  /etc/sluice          profiles.toml (§4) + sluice.env (Provider-Keys, §7.3)   │
└───────────────────────────────────────────────────────────────────────────────┘
```

| Pfad | Inhalt | Eigentümer / Rechte |
|---|---|---|
| `/opt/sluice` | Repo-Checkout + `.venv` | `sluice:sluice`, read-only im Betrieb |
| `/etc/sluice/profiles.toml` | Konsumenten-Profile (§4) | `root:sluice`, `640` |
| `/etc/sluice/sluice.env` | Provider-API-Keys (§7.3) | `root:root`, `600` |
| `/etc/systemd/system/sluice.service` | Unit (aus `deploy/`) | `root:root`, `644` |
| `/etc/systemd/system/sluice-gateway@.service` | Template-Unit der Provider-Gateways (§5.1) | `root:root`, `644` |
| `/etc/sluice/gateway-<provider>.env` | Host/Port + Key je Gateway (§5.1) | `root:root`, `600` |

---

## 1. Voraussetzungen

- VM mit Debian 12 / Ubuntu 24.04 (1 vCPU / 1 GiB RAM reichen für den Start; Sluice ist
  I/O-gebunden, nicht CPU-gebunden).
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

Eigener System-User ohne Login-Shell — der Service läuft nie als root:

```bash
sudo useradd --system --home /opt/sluice --shell /usr/sbin/nologin sluice
```

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

Dann venv anlegen und installieren:

```bash
sudo python3 -m venv /opt/sluice/.venv
sudo /opt/sluice/.venv/bin/pip install /opt/sluice
sudo chown -R sluice:sluice /opt/sluice
```

Kurztest (noch ohne Profile — erwartet ist Default-Deny, kein Fehler):

```bash
sudo -u sluice /opt/sluice/.venv/bin/python -c "import sluice.server; print('ok')"
```

---

## 4. Konfiguration

### 4.1 Profile — `/etc/sluice/profiles.toml` (§4)

**Ohne diese Datei startet Sluice im Default-Deny (§4.3): der Service läuft, blockiert aber
jeden Egress.** Das ist Absicht (fail-closed), keine Störung.

Jedes Projekt bekommt **ein** Profil. Startbeispiel:

```toml
# Irreversibel ist der Default (Rev. 4, safety first) — strategy kann entfallen.
[profile."temper"]
strategy            = "generalizing"
egress_enabled      = true
allowed_purposes    = ["external_escalation", "promotion_upload"]
provider_allowlist  = ["anthropic", "gemini"]
detector_profile    = "infra"

# Reversibel ist explizites Opt-in (§2) — nur wo die Rückübersetzung gebraucht wird.
[profile."aider-code"]
strategy            = "pseudonymizing"
egress_enabled      = true
allowed_purposes    = ["code_completion"]
provider_allowlist  = ["anthropic"]
detector_profile    = "code"
  [profile."aider-code".reversible]
  scope             = "session"
  ttl_seconds       = 3600
  storage           = "memory"
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

Zwei Varianten (Vorlage mit Kommentaren: `deploy/sluice.env.example`):

**Variante A — Dispatch über die eigenständigen Provider-Gateways (Rev. 6, empfohlen):**
pro Provider die Gateway-URL; der Kern braucht dann **keine** Provider-Keys — die liegen
nur bei den Gateways (Schritt 5.1):

```bash
SLUICE_GATEWAY_ANTHROPIC_URL=http://192.168.87.40:17890
SLUICE_GATEWAY_OPENAI_URL=http://192.168.87.40:17891
SLUICE_GATEWAY_GEMINI_URL=http://192.168.87.40:17892
SLUICE_GATEWAY_MISTRAL_URL=http://192.168.87.40:17893
# optional, muss dann auch in jeder gateway-<provider>.env stehen:
# SLUICE_GATEWAY_TOKEN=…
```

**Variante B — Kern ruft die Provider direkt** (ohne Gateway-Services); nur die Keys
setzen, deren Provider keine Gateway-URL haben und die Profile per Allowlist erlauben:

```bash
SLUICE_ANTHROPIC_API_KEY=sk-ant-…
SLUICE_OPENAI_API_KEY=sk-…
SLUICE_GEMINI_API_KEY=…
SLUICE_MISTRAL_API_KEY=…
```

Regeln (§7.3): Keys stehen **nie** im Profil-TOML, nie im Repo, nie im Audit-Log. Ein
fehlender Key ist beim Aufruf des jeweiligen Providers ein harter Fehler (HTTP 500) —
**kein** stiller Fallback auf einen anderen Provider. systemd liest die Datei als root und
reicht die Werte in den Prozess; der `sluice`-User selbst kann die Datei nicht lesen.

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

### 5.1 Provider-Gateways: eigenständige Services (§7.3, Rev. 6)

Sluice-Kern und Provider-Gateways sind **getrennte Services mit eigenem Lebenszyklus** —
jedes Gateway kann jederzeit auf einen eigenen Server umziehen, ohne dass sich für die
Konsumenten etwas ändert. Der Kern findet ein Gateway ausschließlich über seine URL.

```
Konsumenten ──:8000──▶ sluice.service (Kern: Gate → Strategie → Verifier → Audit)
                          │ nur nach released=true, via SLUICE_GATEWAY_<P>_URL
                          ├──:17890──▶ sluice-gateway@anthropic ──▶ api.anthropic.com
                          ├──:17891──▶ sluice-gateway@openai    ──▶ api.openai.com
                          ├──:17892──▶ sluice-gateway@gemini    ──▶ generativelanguage…
                          └──:17893──▶ sluice-gateway@mistral   ──▶ api.mistral.ai
```

| Instanz | Port | Env-Datei | Key darin |
|---|---|---|---|
| `sluice-gateway@anthropic` | 17890 | `/etc/sluice/gateway-anthropic.env` | `SLUICE_ANTHROPIC_API_KEY` |
| `sluice-gateway@openai` | 17891 | `/etc/sluice/gateway-openai.env` | `SLUICE_OPENAI_API_KEY` |
| `sluice-gateway@gemini` | 17892 | `/etc/sluice/gateway-gemini.env` | `SLUICE_GEMINI_API_KEY` |
| `sluice-gateway@mistral` | 17893 | `/etc/sluice/gateway-mistral.env` | `SLUICE_MISTRAL_API_KEY` |

Eigenschaften:

- **Die Boundary bleibt im Kern.** Ein Gateway wird nur vom Sluice-Dispatch aufgerufen,
  *nachdem* der Verifier released hat — es sieht nie Rohtext. Deshalb: Gateways sind
  **interne Dienste**, eingehend nur vom Sluice-Kern erreichbar (Firewall), **nie** direkt
  von Konsumenten. Optional zusätzlich Shared Secret `SLUICE_GATEWAY_TOKEN` (beide Seiten).
- **Key-Isolation:** jedes Gateway hält nur den Key seines Providers; der Kern braucht
  bei dieser Variante **gar keine** Provider-Keys mehr.
- **Umzugsfähig:** zieht ein Gateway auf einen eigenen Server, ändern sich nur
  `SLUICE_HOST` in dessen `gateway-<provider>.env` und die `SLUICE_GATEWAY_<P>_URL`
  in der `sluice.env` des Kerns — sonst nichts.

**Installation der Gateways** (zunächst auf derselben VM 8740):

```bash
sudo cp /opt/sluice/deploy/sluice-gateway@.service /etc/systemd/system/
for p in anthropic openai gemini mistral; do
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
Provider ohne Gateway-URL ruft der Kern weiterhin direkt (dann braucht er deren Key).
Betrieb/Logs je Gateway: `journalctl -u sluice-gateway@anthropic -f`.

**Umzug eines Gateways auf einen eigenen Server:** Schritte 1–3 dieses Dokuments auf dem
neuen Server wiederholen (User, Code, venv — Profile braucht ein Gateway nicht), nur die
eine `gateway-<provider>.env` mit angepasstem `SLUICE_HOST` anlegen, Gateway starten,
im Kern die `SLUICE_GATEWAY_<P>_URL` umstellen, `systemctl restart sluice`. Firewall:
Gateway-Port eingehend nur von der Sluice-Kern-IP; ausgehend 443 nur zum eigenen
Provider-Host (Tabelle in Schritt 6).

---

## 6. Firewall — Sluice als einziger Egress-Pfad

Netzwerkseitig wird das DSGVO-Argument (§1) erst rund: **nur** diese VM darf zu den
Provider-APIs hinaus, und hinein darf nur der Perimeter.

Eingehend:
- TCP `8000` **nur** aus dem internen Netz der Konsumenten (z. B. `192.168.87.0/24`).
- Gateway-Ports `17890–17893` **nur** von der IP des Sluice-Kerns (solange beide auf
  derselben VM laufen, reicht localhost/VM-intern) — Konsumenten sprechen **nie** direkt
  mit einem Gateway (§5.1).
- SSH nach eigenem Admin-Standard.

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

**7.5 Voll-Proxy mit echtem Provider (§7.2, benötigt gültigen Key):**

```bash
curl -s -X POST http://192.168.87.40:8000/v1/chat/completions \
  -H 'content-type: application/json' \
  -H 'X-Sluice-Profile: aider-code' \
  -H 'X-Sluice-Scope: abnahme-test-1' \
  -d '{"messages":[{"role":"user","content":"Antworte mit genau einem Wort: Bereit?"}],
       "model":"claude-sonnet-5"}'
# → {"object":"chat.completion",…"content":"Bereit"…}
```

Erst wenn 7.1–7.5 wie beschrieben antworten, Konsumenten auf die VM zeigen lassen.

---

## 8. Betrieb

**Logs** (strukturiert; jede Egress-Entscheidung erscheint mit Profil, Purpose, Strategie,
released/blocked und Verifier-Findings):

```bash
journalctl -u sluice -f
journalctl -u sluice --since today | grep "released=False"   # heutige Blocks
```

**Update einspielen:**

```bash
cd /opt/sluice
sudo -u sluice git pull                       # bzw. rsync wie in Schritt 3B
sudo /opt/sluice/.venv/bin/pip install /opt/sluice
sudo systemctl restart sluice
curl -s http://192.168.87.40:8000/v1/health   # Abnahme 7.1 wiederholen
```

**Backup:** Nur `/etc/sluice/` sichern (Profile + Keys). `/opt/sluice` ist aus dem Repo
reproduzierbar; der Service selbst hält keinen persistenten Zustand (Mappings sind
in-memory mit TTL, §8 — ein Neustart verwirft sie absichtlich).

**Neuen Konsumenten anschließen:** Profil in `profiles.toml` ergänzen (ggf. Key in
`sluice.env`), `systemctl restart sluice`, Abnahme 7.2/7.4 fürs neue Profil wiederholen.

---

## 9. Troubleshooting

| Symptom | Ursache | Abhilfe |
|---|---|---|
| `/v1/health` zeigt `"profiles":0` | `SLUICE_PROFILES` zeigt ins Leere / TOML-Fehler | `journalctl -u sluice` — Ladefehler brechen den Start mit Ursache ab; Pfad + TOML prüfen |
| Alles antwortet 403 „Default-Deny" | `X-Sluice-Profile`-Header fehlt oder Profilname unbekannt | Header setzen; Name muss exakt dem TOML-Key entsprechen |
| 403 „Purpose … nicht erlaubt" | `purpose` fehlt im Profil | `allowed_purposes` ergänzen oder korrektes `purpose`-Feld senden |
| 403 „Provider … nicht in der Allowlist" | Request-`provider` nicht freigegeben (§4.1) | Allowlist erweitern — bewusste Entscheidung, Provider divergieren in Retention/Training |
| 403 „Verifier blockiert: …" | Roher Identifier im Egress-Kandidaten | **Kein Sluice-Fehler — der Riegel arbeitet.** Konsument muss besser generalisieren, oder reversiblen Modus (Opt-in) nutzen |
| 500 `sluice_provider_config` | API-Key fehlt / Provider-Name unbekannt | `sluice.env` prüfen, Service neu starten |
| 502 `sluice_provider_upstream` | Provider-API down oder Key ungültig | `journalctl` zeigt den HTTP-Status des Providers; Key/Status-Seite des Providers prüfen |
| 502 mit `gateway …` in der reason | Gateway-Service down / URL falsch / Token-Mismatch | `systemctl status sluice-gateway@<p>`; `SLUICE_GATEWAY_<P>_URL` und `SLUICE_GATEWAY_TOKEN` auf beiden Seiten prüfen |
| Service startet nicht | Python < 3.11, venv kaputt, Port belegt | `journalctl -u sluice -n 50`; `ss -tlnp | grep 8000` |

**Grundsatz bei jeder Störung:** Sluice ist fail-closed. Jeder Fehlerpfad blockiert, statt
Rohtext durchzulassen — eine „hängende" Integration ist immer ein Konfigurations- oder
Erreichbarkeitsproblem, nie ein stiller Datenabfluss.
