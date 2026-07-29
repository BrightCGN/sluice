# Profil-Referenz — `/etc/sluice/profiles.toml`

Vollständige Feld-Referenz für Administratoren, die eine **weitere App** an Sluice anschließen.
Betriebsablauf (Installation, systemd, Gateways) steht in `DEPLOY.md`; die Design-Begründungen
stehen in `SLUICE-BOUNDARY-SPEC.md` — **bei Widerspruch gewinnt die Spec.** Diese Datei
beschreibt, was der Code heute tut, mit Quellenangabe.

Ein Profil ist die **vollständige, auditierbare Form der Egress-Erlaubnis einer App** (§4).
Eine App = ein Profil. Ohne passendes Profil geht nichts raus (Default-Deny, §4.3).

---

## 1. Datei, Rechte, Reload

| | |
|---|---|
| Pfad | `/etc/sluice/profiles.toml` (Pfad überschreibbar per `SLUICE_PROFILES`) |
| Eigentümer/Rechte | `root:sluice`, `640` |
| Format | TOML, eine Tabelle `[profile."<name>"]` je App |
| Laden | beim Start; **Ladefehler brechen den Start ab** (fail-closed, `policy.py:132`) |
| Reload | `sudo systemctl restart sluice` — keine Live-Neuladung |
| Prüfen | `curl …/v1/health` → `{"profiles": N}`; `N = 0` heißt: Datei prüfen |

Der Profilname ist der Schlüssel, den die App im Header `X-Sluice-Profile` (bzw. Feld `profile`
bei `/v1/egress/guard`) mitschickt. Unbekannter Name ⇒ kein Profil ⇒ Default-Deny, **kein**
Fallback auf ein anderes Profil (§4.3).

---

## 2. Feld-Referenz

Maßgeblich ist `sluice/policy.py` (`Profile`, `parse_profiles`).

| Feld | Typ | Default | Wirkung |
|---|---|---|---|
| `mode` | String | `"strict"` | Sanitisierungs-Modus aus der Registry (§3). Fehlt das Feld, gilt **`strict`**, nie `passthrough`. |
| `egress_enabled` | Bool | `true` | `false` = souveränes/air-gapped Profil: **nichts raus, egal welcher Modus** (§4.2). Der Riegel greift vor der Modus-Auswahl. |
| `allowed_purposes` | Liste | `[]` | Erlaubte Verwendungszwecke. **Leer = alles blockiert** (siehe Fallstrick 3). |
| `provider_allowlist` | Liste | `[]` | Erlaubte Provider (§4.1). Leer = kein Provider erlaubt. |
| `allowed_modes` | Liste | `[]` | Begrenzt die wählbaren Modi. Leer hat **zwei** Bedeutungen — siehe §4. |
| `detector_profile` | String | `"infra"` | Muster-Set des Verifiers (§5). Bei `passthrough` wirkungslos. |
| `dictionary_terms` | Liste | `[]` | Konsument-deklarierte Literale, die Regex nicht fängt (§5.1, Rev. 10). |
| `[…​.reversible]` | Tabelle | — | Mapping-Lebenszyklus; **nur bei `mode = "pseudonymizing"` geparst** (Fallstrick 4). |

`strategy` bleibt als Parse-Alias für `mode` bestehen (Rev. 9, `policy.py:140`) — für neue
Profile nicht mehr verwenden.

---

## 3. Modi

| Modus | Verifier | Reversibel | Wofür |
|---|---|---|---|
| `strict` | ja | nein | **Default.** Die App schickt Rohtext, Sluice redigiert selbst (`[EMAIL]`/`[IP]`/`[NAME]`), Antwort braucht keine Rückübersetzung. |
| `generalizing` | ja | nein | Die App hat **selbst schon generalisiert**; Sluice verifiziert nur noch. Die Generalisierung ist Domäne der App, nicht von Sluice (§1.1). |
| `pseudonymizing` | ja | **ja** | Rückübersetzung nötig (Tool-Argumente, Streaming-Antworten). Explizites Opt-in — die Mapping-Tabelle bleibt personenbezogen. |
| `passthrough` | **nein** | nein | Kein Verifier, keine Transformation (§2.1). Nur Profil-Gate + Audit greifen. Braucht `allowed_modes`-Opt-in. |

**Auswahlhilfe:** Schickt die App Rohtext und will nur „sauber raus"? → `strict`. Braucht sie die
Provider-Antwort mit den echten Werten zurück? → `pseudonymizing`. Generalisiert sie bereits
selbst? → `generalizing`. Alles andere → `strict`.

---

## 4. `allowed_modes` — die Regel, die am ehesten schiefgeht

Zwei Schranken, beide fail-closed (`policy.py:99`, Rev. 11):

1. **Ist `allowed_modes` gesetzt, muss der Modus darin stehen** — sonst 403.
2. **Ein Modus ohne Verifier** (heute nur `passthrough`) ist **nur** erlaubt, wenn er
   *ausdrücklich* in `allowed_modes` steht. Leere/fehlende Liste **sperrt** ihn: „Vergessen = zu".

Daraus folgt die Wahrheitstabelle:

| `allowed_modes` | `strict` / `generalizing` / `pseudonymizing` | `passthrough` |
|---|---|---|
| nicht gesetzt / `[]` | erlaubt | **403** |
| `["passthrough"]` | **403** | erlaubt |
| `["strict", "passthrough"]` | nur `strict` erlaubt | erlaubt |

> **Fallstrick 1 — der häufigste Fehler:** Sobald du `allowed_modes` setzt, musst du den
> **Profil-Modus selbst mit aufnehmen.** `mode = "strict"` zusammen mit
> `allowed_modes = ["passthrough"]` blockiert den Normalbetrieb des Profils komplett.

Die Regel greift generisch am Modus-Merkmal (`enforce_verifier`), nicht am Namen `passthrough` —
ein selbst registrierter fail-open-Modus (§3) verhält sich identisch.

---

## 5. `detector_profile` und `dictionary_terms`

Muster-Sets aus `sluice/detectors/`:

| Name | Fängt |
|---|---|
| `infra` | IPv4, E-Mail, interne Hostnamen (`*.internal/.local/.corp/.lan/.intra`), FQDN, `/home/<user>`, Secrets/Tokens |
| `code` | Git-Remotes, Repo-Pfade, Env-Werte |
| `media` | Share-/Netzwerkpfade |
| `financial` | IBAN, BIC, Kontonummern |

`dictionary_terms` ergänzt das um **literale** Begriffe, die keine Regex erkennt — freie
Personennamen, Straßen, Hausnamen. Die Terme werden regex-escaped und wortgrenzen-gebunden
ersetzt (Platzhalter `[NAME]`).

> **Fallstrick 2:** `dictionary_terms` steht im Klartext in der Profildatei. Das ist der Grund
> für `640 root:sluice` — die Liste ist selbst personenbezogen. Nicht in Git einchecken.

> **Fallstrick 3 — nicht beim Laden geprüft:** Ein **Tippfehler im `detector_profile`-Namen**
> bricht den Start *nicht* ab. `parse_profiles` validiert nur `mode` und `allowed_modes` gegen
> die Registry; ein unbekanntes Detektor-Profil fällt erst zur Laufzeit auf — der Verifier
> findet kein Muster-Set und **blockt fail-closed** (`verifier.py:52`). Symptom: Profil lädt
> sauber, aber *jeder* Request wird blockiert; im Audit steht dann wörtlich
> `unbekanntes Detektor-Profil '<name>' (fail-closed)`. Namen genau prüfen.

---

## 6. `[profile."…".reversible]`

Nur bei `mode = "pseudonymizing"` (`policy.py:150`).

| Feld | Default | Zulässig |
|---|---|---|
| `scope` | `"session"` | frei — der Isolationsschlüssel der Mapping-Tabelle |
| `ttl_seconds` | `3600` | Ganzzahl; danach verfällt das Mapping |
| `storage` | `"memory"` | **nur `"memory"`** — alles andere bricht den Start ab (persistent ist post-v1, §8/§10) |

Der Scope kommt pro Request aus `X-Sluice-Scope` (bzw. `scope` im Guard-Body); ohne Angabe
läuft alles im Default-Scope `global` — Sessions sind dann **nicht** gegeneinander isoliert.

> **Fallstrick 4:** Der `reversible`-Block wird **nur geparst, wenn der Profil-Modus
> `pseudonymizing` ist.** Wählt eine App `pseudonymizing` erst per Request-Override (§7) über
> einem `strict`-Profil, greifen die stillen Defaults (TTL 3600 s), *nicht* deine Werte. Wer
> den Lebenszyklus steuern will, setzt `mode = "pseudonymizing"` im Profil.

---

## 7. Was das Profil festlegt und was die App wählen darf

Die App kann pro Request abweichen — **jede Wahl läuft trotzdem durchs Gate** (§4.1):

| Request | Wirkung | Grenze |
|---|---|---|
| `mode` im Body | überschreibt den Profil-Modus (§7.2) | muss `allowed_modes` genügen, sonst 403; unbekannter Name 400 |
| `purpose` | wählt den Zweck | muss in `allowed_purposes` stehen; fehlt er, gilt **der erste Eintrag** |
| `provider` | wählt den Provider | muss in `provider_allowlist` stehen; fehlt er, gilt **der erste Eintrag** |
| `X-Sluice-Scope` | Mapping-Isolation | — |

Modus-Aliase im Request (`server.py:66`): `reversible` → `pseudonymizing`,
`irreversible` → `generalizing`.

> **Fallstrick 5:** Weil `purpose` und `provider` auf den **ersten Listeneintrag** defaulten, ist
> die **Reihenfolge in den Listen bedeutsam.** Der erste Provider in `provider_allowlist` ist der
> De-facto-Standard-Provider dieser App.

---

## 8. Provider-Allowlist

Gültige Namen: `anthropic` (Alias `claude`), `openai`, `gemini`, `mistral`
(`providers/__init__.py:97`). Andere Namen ⇒ fail-closed.

Ein Eintrag in der Allowlist genügt **nicht** — der Kern ruft Provider nie direkt (Rev. 7).
Für jeden gelisteten Provider muss zusätzlich gelten:

1. Die Gateway-Instanz läuft: `systemctl start sluice-gateway@<provider>`
2. `SLUICE_GATEWAY_<PROVIDER>_URL` steht in `/etc/sluice/sluice.env`

Fehlt die URL, ist das ein Konfigurationsfehler: **HTTP 500 `sluice_provider_config`**, kein
stiller Ausweichen auf einen anderen Provider. Läuft die Kern-Instanz mit `SLUICE_PROVIDER`
(Provider-Lock, Rev. 5), werden fremde Provider zusätzlich mit 403 abgewiesen.

---

## 9. Kochbuch: neue App anschließen

1. **Profilnamen festlegen** — kurz, stabil; die App schickt ihn als `X-Sluice-Profile`.
2. **Modus wählen** (§3-Tabelle oben). Im Zweifel `strict`.
3. **Zwecke benennen.** Ein Zweck je fachlichem Egress-Pfad, nicht einer für alles — sie sind
   die Audit-Granularität. Ersten Eintrag bewusst wählen (Fallstrick 5).
4. **Provider setzen** — nur die, deren Retention/Training-Bedingungen für diese Daten geprüft
   sind. Gateways dafür starten (§8).
5. **Detektor-Profil wählen**, Name exakt schreiben (Fallstrick 3). Freie Namen/Adressen in
   `dictionary_terms`.
6. **Nur bei `pseudonymizing`:** `reversible`-Block mit TTL.
7. **`allowed_modes` nur setzen, wenn du wirklich einschränken willst** — dann Profil-Modus
   mit aufnehmen (Fallstrick 1). Für `passthrough` ist es Pflicht.
8. `systemctl restart sluice`, dann `/v1/health` prüfen: Profilzahl gestiegen?
9. **Gegenprobe fahren:** einen Request mit einem verbotenen `purpose` und einen mit einem
   nicht gelisteten `provider` schicken — beide müssen blockiert werden. Ein Profil, das nie
   blockiert hat, ist ungetestet.

---

## 10. Vollständiges Beispiel

```toml
# --- Rohtext rein, Sluice redigiert selbst. Der Normalfall. -------------------
[profile."meine-app"]
mode                = "strict"
egress_enabled      = true
allowed_purposes    = ["support_summary", "external_escalation"]
provider_allowlist  = ["anthropic", "gemini"]   # anthropic = Default (erster Eintrag)
detector_profile    = "infra"
dictionary_terms    = ["<kundenname>", "<strasse hausnr>"]

# --- Rückübersetzung nötig (Tool-Argumente, Streaming). Opt-in. ---------------
[profile."meine-app-code"]
mode                = "pseudonymizing"
egress_enabled      = true
allowed_purposes    = ["code_completion"]
provider_allowlist  = ["anthropic"]
detector_profile    = "code"
  [profile."meine-app-code".reversible]
  scope             = "session"
  ttl_seconds       = 3600
  storage           = "memory"

# --- Abgeschaltet: läuft, lässt aber nichts durch (§4.2). --------------------
[profile."meine-app-air-gapped"]
egress_enabled      = false                     # mode entfällt → strict
allowed_purposes    = []
provider_allowlist  = []
```

---

## 11. Fehlerbilder

| Symptom | Ursache | Prüfen |
|---|---|---|
| Service startet nicht | unbekannter `mode`, unbekannter Eintrag in `allowed_modes`, `storage != "memory"` | `journalctl -u sluice` — der Ladefehler nennt Profil und Feld |
| `/v1/health` zeigt `"profiles": 0` | `SLUICE_PROFILES` zeigt ins Leere, oder TOML-Syntaxfehler | Pfad und Datei prüfen |
| **Jeder** Request blockiert, Profil lädt aber | Tippfehler im `detector_profile` (Fallstrick 3) oder `allowed_purposes` leer | Namen gegen §5 prüfen |
| 403 trotz korrekt gesetztem `mode` | `allowed_modes` gesetzt, Profil-Modus fehlt darin (Fallstrick 1) | Wahrheitstabelle §4 |
| 403 bei `passthrough` | fehlendes `allowed_modes`-Opt-in (Rev. 11) | `allowed_modes = ["passthrough"]` |
| 500 `sluice_provider_config` | `SLUICE_GATEWAY_<PROVIDER>_URL` fehlt | `/etc/sluice/sluice.env`, Gateway-Dienst läuft? |
| Egress geht an den falschen Provider | App schickt kein `provider` → erster Allowlist-Eintrag (Fallstrick 5) | Reihenfolge in `provider_allowlist` |
