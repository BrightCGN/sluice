#!/usr/bin/env bash
#
# Sluice — Provisionierung der Sluice-VM (ID 8740, 192.168.87.40).
#
# Legt den kompletten Stack an: Kern-Service (sluice.service) + je ein eigenständiges
# Provider-Gateway (sluice-gateway@<provider>), jedes unter eigenem System-User
# (Rev. 8, User-Isolation). Idempotent gedacht: mehrfaches Ausführen soll nichts
# kaputt machen — bestehende Konfiguration in /etc/sluice wird NIE überschrieben.
#
# Optional der NER-Dienst (Rev. 12, §7.5) — bewusst OPT-IN, weil er Modell-Gewichte
# und (im torch-Pfad) über ein Gigabyte Abhängigkeiten mitbringt, die eine VM ohne
# `pii_ner`-Profil nichts angehen:
#   SLUICE_WITH_NER=1 bash deploy/bootstrap.sh
#
# Voraussetzung: Debian 12 / Ubuntu 24.04 mit root/sudo, Netz, Python >= 3.11.
# Aufruf (als root):   bash deploy/bootstrap.sh
#
# Fail-closed by design (§4.3): das Skript startet die Dienste NICHT automatisch.
# Ohne ausgefüllte profiles.toml und Gateway-Keys wäre das nur ein Service, der
# alles blockiert — die Freigabe ist bewusst ein manueller, letzter Schritt.
#
# Bezug: docs/DEPLOY.md (Schritt 1–5), docs/SLUICE-BOUNDARY-SPEC.md (§4, §5, §7.3, §7.5).

set -euo pipefail

APP_DIR="/opt/sluice"
CFG_DIR="/etc/sluice"
SERVICE_USER="sluice"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Gateways, die auf DIESER Maschine laufen sollen. Beim Umzug eines Gateways auf
# einen eigenen Server dort nur den einen Provider setzen:
#   SLUICE_PROVIDERS="anthropic" bash deploy/bootstrap.sh
PROVIDERS="${SLUICE_PROVIDERS:-anthropic openai gemini mistral}"

# Bind-Adresse des Kerns. Weicht sie vom Spec-Default ab, werden die INSTALLIERTEN
# Units/Env-Dateien angepasst (die Repo-Dateien bleiben unberührt).
DEFAULT_HOST="192.168.87.40"
BIND_HOST="${SLUICE_BIND_HOST:-$DEFAULT_HOST}"

# NER-Dienst (§7.5) — nur für Profile mit `mode = "pii_ner"`. Standardmäßig AUS: die
# übrigen Modi (strict, pii_regex, generalizing, pseudonymizing, passthrough) brauchen
# ihn nicht, und ein Kern ohne Modell-Abhängigkeiten ist betrieblich der schlankere.
WITH_NER="${SLUICE_WITH_NER:-0}"
NER_USER="sluice-ner"
# Welcher Extra installiert wird. `ner` (torch) ist der Default; die ONNX-Varianten erst
# NACH der Messung wählen — scripts/probe_ner_hardware.py, docs/NER-SERVICE.md.
NER_EXTRA="${SLUICE_NER_EXTRA:-ner}"

# torch von PyPI zieht auf Linux die CUDA-Variante mit der kompletten nvidia-Laufzeit —
# mehrere GB, auch wenn nie eine GPU im Spiel ist. Weil `cpu` der Auslieferungs-Default
# ist (ein Chokepoint ohne GPU-Abhängigkeit ist betrieblich robuster), installiert das
# Skript torch aus dem CPU-Index. Wer wirklich CUDA will, setzt SLUICE_NER_TORCH_CPU=0.
TORCH_CPU="${SLUICE_NER_TORCH_CPU:-1}"
TORCH_CPU_INDEX="https://download.pytorch.org/whl/cpu"

log()  { printf '\033[1;34m[bootstrap]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[bootstrap]\033[0m %s\n' "$*" >&2; }

if [[ "${EUID}" -ne 0 ]]; then
    echo "Bitte als root ausführen (sudo bash deploy/bootstrap.sh)." >&2
    exit 1
fi

if [[ "${WITH_NER}" == "1" ]]; then
    case "${NER_EXTRA}" in
        ner|ner-onnx|ner-onnx-gpu) ;;
        *)
            echo "SLUICE_NER_EXTRA='${NER_EXTRA}' unbekannt — erlaubt: ner, ner-onnx, ner-onnx-gpu." >&2
            exit 1
            ;;
    esac
fi

# --- 1. System-Pakete --------------------------------------------------------
# Sluice ist I/O-gebunden und hat keine nativen Abhängigkeiten — die Liste ist kurz.
log "Installiere System-Pakete …"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    git rsync curl ca-certificates

PY_VER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "Python ${PY_VER} ist zu alt — Sluice braucht >= 3.11 (pyproject requires-python)." >&2
    exit 1
fi
log "Python ${PY_VER} ok."

# --- 2. Service-User ---------------------------------------------------------
# Kern und JEDES Gateway bekommen einen eigenen User (Rev. 8): kein Gateway kann
# Dateien oder Speicher eines anderen — oder des Kerns — lesen. Beim Umzug eines
# Gateways auf einen eigenen Server wandert genau ein User mit.
#
# Der NER-Dienst bekommt aus demselben Grund einen eigenen User — mit dem Unterschied,
# dass er als einziger Dienst *unsanitisierten* Rohtext sieht (§7.5). Ihn vom Kern zu
# trennen heißt: das Modell ist tauschbar, ohne den Chokepoint anzufassen.
NER_USERS=""
[[ "${WITH_NER}" == "1" ]] && NER_USERS="${NER_USER}"

for u in "${SERVICE_USER}" $(for p in ${PROVIDERS}; do echo "sluice-gw-${p}"; done) ${NER_USERS}; do
    if ! id "${u}" &>/dev/null; then
        log "Lege Service-User '${u}' an …"
        useradd --system --home "${APP_DIR}" --shell /usr/sbin/nologin "${u}"
    fi
done
mkdir -p "${APP_DIR}" "${CFG_DIR}"

# --- 3. Code nach /opt/sluice ------------------------------------------------
if [[ "${REPO_DIR}" != "${APP_DIR}" ]]; then
    log "Kopiere Repo nach ${APP_DIR} …"
    # --delete hält /opt/sluice deckungsgleich mit dem Checkout; .venv ist
    # ausgenommen, damit ein Update nicht die Installation wegräumt.
    #
    # 'models' aus demselben Grund, und der wiegt schwerer: der HF-Modell-Cache (§7.5)
    # entsteht NUR auf der Zielmaschine und hat im Repo kein Gegenstück — --delete würde
    # ihn also bei jedem Deploy aus einem Checkout außerhalb von ${APP_DIR} restlos
    # löschen. Gigabytes, ein erneuter Download hinter der Firewall, und der NER-Dienst
    # kommt bis dahin nicht hoch (fail-closed: pii_ner-Profile blockieren mit 503).
    rsync -a --delete \
        --exclude '.git' --exclude '__pycache__' --exclude '.venv' \
        --exclude '.pytest_cache' --exclude 'models' \
        "${REPO_DIR}/" "${APP_DIR}/"
fi

# --- 4. venv + Installation --------------------------------------------------
# Ein venv ist NICHT verschiebbar: die Konsolen-Skripte in bin/ tragen den absoluten
# Interpreterpfad in ihrer Shebang-Zeile. Wird ein venv kopiert oder von einer anderen
# Maschine gersynct, zeigen sie weiter auf das alte Verzeichnis — und der Dienst scheitert
# mit „Permission denied" auf das SKRIPT, obwohl dessen Rechte stimmen und der Interpreter
# gemeint ist. (Zeigt der tote Pfad unter ein fremdes Home mit 0700, wird aus dem
# eigentlich fälligen ENOENT sogar ein EACCES — die Meldung führt dann doppelt in die Irre.)
# Deshalb: nicht nur auf Existenz prüfen, sondern auf Zugehörigkeit zu DIESEM Verzeichnis.
venv_is_sane() {
    local py="${APP_DIR}/.venv/bin/python"
    [[ -x "${py}" ]] || return 1
    "${py}" -c 'import sys' 2>/dev/null || return 1
    # pip stellvertretend für alle Konsolen-Skripte: sein Shebang muss hierher zeigen.
    [[ -f "${APP_DIR}/.venv/bin/pip" ]] || return 1
    head -1 "${APP_DIR}/.venv/bin/pip" | grep -q "^#!${APP_DIR}/\.venv/" || return 1
}

if [[ -d "${APP_DIR}/.venv" ]] && ! venv_is_sane; then
    warn "Das venv unter ${APP_DIR}/.venv gehört nicht hierher (fremde Shebang-Pfade
  oder defekter Interpreter) — vermutlich von einer anderen Maschine kopiert. Ein venv
  ist nicht verschiebbar; ich baue es neu. Installierte Extras werden dabei mit
  neu installiert."
    rm -rf "${APP_DIR}/.venv"
fi
if [[ ! -d "${APP_DIR}/.venv" ]]; then
    log "Erzeuge venv …"
    python3 -m venv "${APP_DIR}/.venv"
fi
# Rechte über den ganzen Baum geradeziehen. Als Funktion und nicht als einmalige Zeile,
# weil sie nach JEDEM pip-Lauf gelten muss: pip legt Dateien mit der umask von root an,
# und ist die eng (0077), sind die neu geschriebenen Konsolen-Skripte — darunter
# .venv/bin/uvicorn — für den Service-User nicht ausführbar. Der Dienst scheitert dann
# mit 203/EXEC „Permission denied", bevor Python überhaupt startet.
normalize_app_perms() {
    [[ -d "${APP_DIR}" ]] || return 0
    # Kern-User besitzt den Baum; die Gateway-User teilen sich NUR den Code (o+rX),
    # sonst nichts (DEPLOY.md §3). Provider-Keys liegen in /etc/sluice, nie hier.
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"
    chmod -R a+rX "${APP_DIR}"

    # Der Modell-Cache ist die Ausnahme: das rekursive a+rX oben würde seine 700
    # aufreißen und das chown ihn dem Kern-User zuschlagen, worauf der NER-Dienst ihn
    # nicht mehr aktualisieren könnte. Deshalb danach, und rekursiv.
    #
    # Bedingung ist die EXISTENZ des Verzeichnisses, nicht WITH_NER. Sonst räumt ein
    # Kern-Bootstrap (ohne SLUICE_WITH_NER=1) auf einer Maschine MIT NER-Dienst dessen
    # Rechte still ab: das chown/chmod oben läuft ja unbedingt, nur die Korrektur hier
    # bliebe aus. Der NER-User verliert damit den Schreibzugriff auf seinen eigenen
    # Cache — und das fällt erst beim nächsten Modell-Download auf, nicht beim Lauf.
    if [[ -d "${APP_DIR}/models" ]] && id "${NER_USER}" &>/dev/null; then
        chown -R "${NER_USER}:${NER_USER}" "${APP_DIR}/models"
        chmod -R go-rwx "${APP_DIR}/models"
    fi
}
# Sicherheitsnetz: auch ein Abbruch mitten im Skript darf den Baum nicht in einem
# Zustand hinterlassen, in dem der Kern nicht mehr startet.
trap normalize_app_perms EXIT

# EDITIERBAR (-e), damit es genau EINE Codequelle gibt. Eine normale Installation legt
# eine zweite Kopie unter site-packages/sluice/ an, und welche der beiden gilt, hängt dann
# am Arbeitsverzeichnis: uvicorn stellt mit --app-dir (Default "") das cwd an den Anfang
# von sys.path, und alle drei Units setzen WorkingDirectory=/opt/sluice. Die Dienste laufen
# damit aus dem Quellbaum, jedes Skript mit anderem cwd aber aus der Kopie — die nach einem
# reinen Datei-Deploy (rsync/tar ohne pip) veraltet ist, ohne dass irgendetwas fehlschlägt.
# Ein Chokepoint, dessen Version vom Arbeitsverzeichnis abhängt, ist keiner.
log "Installiere Sluice (editierbar) …"
"${APP_DIR}/.venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/.venv/bin/pip" install --quiet -e "${APP_DIR}"

# Reste einer früheren, nicht-editierbaren Installation müssen weg. pip räumt sie beim
# Umstieg normalerweise selbst ab — aber nur, solange seine RECORD-Datei intakt ist. Bleibt
# das Verzeichnis liegen, verdeckt es den Quellbaum weiterhin: der Pfad aus dem .pth-File
# wird NACH site-packages in sys.path eingehängt, die Kopie gewinnt also. Genau die
# Zweideutigkeit, die dieser Schritt beseitigen soll — deshalb hier hart nachsehen.
SITE_PKGS="$("${APP_DIR}/.venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
if [[ -d "${SITE_PKGS}/sluice" ]]; then
    warn "Entferne Kopie einer früheren (nicht-editierbaren) Installation:"
    warn "  ${SITE_PKGS}/sluice — sie würde ${APP_DIR}/sluice verdecken."
    rm -rf "${SITE_PKGS}/sluice"
fi

# Sofort normalisieren, nicht erst am Ende von Abschnitt 4: ab hier ist der Kern
# startfähig, und alles Folgende — insbesondere der große, fehleranfällige
# NER-Extra-Install — kann ihn nicht mehr mit in den Abgrund ziehen.
normalize_app_perms

# Modell-Abhängigkeiten NUR bei aktivem NER-Dienst. Sie landen zwangsläufig im selben
# venv (ein Interpreter für alle Units) — der Kern *importiert* sie deshalb trotzdem
# nicht: gliner/torch werden erst in sluice.ner.engine lazy geladen, und die läuft nur
# im NER-Prozess. Der Kern-Code bleibt frei von Modell-Abhängigkeiten (§7.5).
if [[ "${WITH_NER}" == "1" ]]; then
    # Vor dem großen Install prüfen, nicht mittendrin auf ENOSPC laufen: eine halb
    # installierte Umgebung ist schlimmer als eine gar nicht installierte, weil pip die
    # Konsolen-Skripte schon neu geschrieben haben kann.
    case "${NER_EXTRA}" in
        ner) need_mib=$(( TORCH_CPU == 1 ? 4096 : 9216 )) ;;
        *)   need_mib=2560 ;;
    esac
    avail_mib=$(( $(df -Pk "${APP_DIR}" | awk 'NR==2 {print $4}') / 1024 ))
    if (( avail_mib < need_mib )); then
        echo "Zu wenig Plattenplatz für '[${NER_EXTRA}]': ${avail_mib} MiB frei, ~${need_mib} MiB nötig." >&2
        echo "  (venv + Modell-Cache unter ${APP_DIR}; das Modell kommt später noch dazu.)" >&2
        if [[ "${NER_EXTRA}" == "ner" && "${TORCH_CPU}" != "1" ]]; then
            echo "  Ohne SLUICE_NER_TORCH_CPU=0 wäre der Bedarf deutlich kleiner — die" >&2
            echo "  CUDA-Variante von torch bringt die komplette nvidia-Laufzeit mit." >&2
        fi
        exit 1
    fi

    install -d -o "${NER_USER}" -g "${NER_USER}" -m 700 "${APP_DIR}/models"

    # torch zuerst und allein aus dem CPU-Index. Nicht per --extra-index-url zusammen mit
    # dem Rest: der CPU-Index führt nur torch & Co., und eine gemischte Auflösung würde
    # unvorhersehbar mal hier, mal dort landen. Ist torch danach erfüllt, installiert der
    # Extra-Schritt nur noch den Rest.
    if [[ "${NER_EXTRA}" == "ner" && "${TORCH_CPU}" == "1" ]]; then
        log "Installiere torch (CPU-Wheels, ohne CUDA-Laufzeit) …"
        "${APP_DIR}/.venv/bin/pip" install --quiet --index-url "${TORCH_CPU_INDEX}" torch
    fi

    # Ebenfalls -e: ohne das würde pip hier eine nicht-editierbare Kopie über die
    # editierbare Installation legen und die Zweideutigkeit von oben wieder einführen.
    log "Installiere NER-Extra '[${NER_EXTRA}]' — das dauert …"
    "${APP_DIR}/.venv/bin/pip" install --quiet -e "${APP_DIR}[${NER_EXTRA}]"
    normalize_app_perms
fi

# --- 5. Konfiguration --------------------------------------------------------
# Grundsatz: nur anlegen, was fehlt. Eine bestehende profiles.toml oder eine
# Gateway-Env mit eingetragenem Key darf ein Re-Run niemals überschreiben.
chown root:root "${CFG_DIR}"
chmod 755 "${CFG_DIR}"

# 5.1 Profile (§4) — ohne diese Datei läuft Sluice im Default-Deny.
if [[ ! -f "${CFG_DIR}/profiles.toml" ]]; then
    if [[ -f "${APP_DIR}/deploy/profiles.toml.example" ]]; then
        log "Lege ${CFG_DIR}/profiles.toml aus der Vorlage an — BITTE ANPASSEN!"
        cp "${APP_DIR}/deploy/profiles.toml.example" "${CFG_DIR}/profiles.toml"
    else
        warn "Keine profiles.toml.example gefunden — lege leere Datei an (Default-Deny)."
        : > "${CFG_DIR}/profiles.toml"
    fi
fi
# root schreibt, der Kern-User liest (enthält Policy, keine Keys).
chown root:"${SERVICE_USER}" "${CFG_DIR}/profiles.toml"
chmod 640 "${CFG_DIR}/profiles.toml"

# 5.2 Kern-Env (§7.3) — Gateway-URLs, KEINE Provider-Keys.
if [[ ! -f "${CFG_DIR}/sluice.env" ]]; then
    log "Lege ${CFG_DIR}/sluice.env aus der Vorlage an …"
    cp "${APP_DIR}/deploy/sluice.env.example" "${CFG_DIR}/sluice.env"
    if [[ "${BIND_HOST}" != "${DEFAULT_HOST}" ]]; then
        sed -i "s/${DEFAULT_HOST}/${BIND_HOST}/g" "${CFG_DIR}/sluice.env"
    fi
    # Läuft der NER-Dienst auf DIESER Maschine, ist die Loopback-URL die richtige und
    # der Rohtext verlässt den Host nie. Nur beim Neuanlegen — eine bestehende
    # sluice.env fasst das Skript grundsätzlich nicht an (siehe Hinweis unten).
    if [[ "${WITH_NER}" == "1" ]]; then
        sed -i 's|^# \(SLUICE_NER_URL=http://127\.0\.0\.1:17900\)$|\1|' "${CFG_DIR}/sluice.env"
    fi
fi
chown root:root "${CFG_DIR}/sluice.env"
chmod 600 "${CFG_DIR}/sluice.env"

# Bestehende sluice.env + nachträglich aktivierter NER-Dienst: nicht editieren, sondern
# sagen. Ohne URL blockiert ein pii_ner-Profil fail-closed (§5.3) — das wäre sonst ein
# Fehlerbild, dessen Ursache der Betreiber erst im Log suchen müsste.
if [[ "${WITH_NER}" == "1" ]] && ! grep -qE '^\s*SLUICE_NER_URL=' "${CFG_DIR}/sluice.env"; then
    warn "In ${CFG_DIR}/sluice.env fehlt SLUICE_NER_URL — pii_ner-Profile blockieren so"
    warn "  fail-closed. Zeile eintragen: SLUICE_NER_URL=http://127.0.0.1:17900"
fi

# 5.3 Gateway-Envs (§5.1) — je Instanz Host/Port + genau EIN Provider-Key.
for p in ${PROVIDERS}; do
    src="${APP_DIR}/deploy/gateway-${p}.env.example"
    dst="${CFG_DIR}/gateway-${p}.env"
    if [[ ! -f "${src}" ]]; then
        warn "Keine Vorlage ${src} — Gateway '${p}' übersprungen."
        continue
    fi
    if [[ ! -f "${dst}" ]]; then
        log "Lege ${dst} an — KEY MUSS NOCH EINGETRAGEN WERDEN!"
        cp "${src}" "${dst}"
        if [[ "${BIND_HOST}" != "${DEFAULT_HOST}" ]]; then
            sed -i "s/${DEFAULT_HOST}/${BIND_HOST}/g" "${dst}"
        fi
    fi
    chown root:root "${dst}"
    chmod 600 "${dst}"
done

# 5.4 NER-Env (§7.5) — Modellidentität, kein Key. Wie überall: nie überschreiben.
if [[ "${WITH_NER}" == "1" ]]; then
    if [[ ! -f "${CFG_DIR}/ner.env" ]]; then
        log "Lege ${CFG_DIR}/ner.env an — MODELL + REVISION MÜSSEN NOCH EINGETRAGEN WERDEN!"
        cp "${APP_DIR}/deploy/ner.env.example" "${CFG_DIR}/ner.env"
    fi
    chown root:root "${CFG_DIR}/ner.env"
    chmod 600 "${CFG_DIR}/ner.env"
    # Ohne festgenagelte Revision ist die Anonymisierungs-Identität (§5.4) wertlos: das
    # Modell-Repo könnte sich unter derselben Kennung ändern, und der Audit-Eintrag
    # behauptete etwas Unbelegtes. Der Dienst startet trotzdem — deshalb hier warnen.
    if ! grep -qE '^\s*SLUICE_NER_REVISION=\S' "${CFG_DIR}/ner.env"; then
        warn "SLUICE_NER_REVISION in ${CFG_DIR}/ner.env ist leer — ohne festgenagelten"
        warn "  Commit-Hash ist die Anonymisierungs-Identität (§5.4) nicht belastbar."
    fi
fi

# --- 6. systemd-Units --------------------------------------------------------
log "Installiere systemd-Units …"
cp "${APP_DIR}/deploy/sluice.service" /etc/systemd/system/
cp "${APP_DIR}/deploy/sluice-gateway@.service" /etc/systemd/system/
if [[ "${BIND_HOST}" != "${DEFAULT_HOST}" ]]; then
    log "Passe Bind-Adresse der installierten Unit auf ${BIND_HOST} an …"
    sed -i "s/${DEFAULT_HOST}/${BIND_HOST}/g" /etc/systemd/system/sluice.service
fi
chmod 644 /etc/systemd/system/sluice.service /etc/systemd/system/sluice-gateway@.service
if [[ "${WITH_NER}" == "1" ]]; then
    # Bind-Adresse NICHT ersetzen: der NER-Dienst hört per Vorlage auf 127.0.0.1 und
    # soll das auch, solange er neben dem Kern läuft — dann verlässt der Rohtext den
    # Host nie. Ein entfernter Betrieb ist eine bewusste Einzelfall-Entscheidung
    # (deploy/ner.env.example, Abschnitt „Entfernter Betrieb").
    cp "${APP_DIR}/deploy/sluice-ner.service" /etc/systemd/system/
    chmod 644 /etc/systemd/system/sluice-ner.service
fi
systemctl daemon-reload

# Die Unit bindet an eine feste IP — fehlt sie auf dieser Maschine, scheitert der
# Start erst später mit "Cannot assign requested address". Lieber jetzt sagen.
if ! ip -o addr show 2>/dev/null | grep -qw "${BIND_HOST}"; then
    warn "Adresse ${BIND_HOST} ist auf dieser Maschine nicht konfiguriert —"
    warn "  sluice.service kann so nicht binden. Statische IP setzen oder"
    warn "  Bootstrap mit SLUICE_BIND_HOST=<ip> erneut laufen lassen."
fi

# Importtest als Service-User: findet kaputte venvs/Rechte vor dem ersten Start.
#
# Bewusst aus '/' heraus (`cd /` im Subshell) und mit Prüfung des AUFGELÖSTEN Pfades.
# Aus ${APP_DIR} heraus wäre der Test wertlos: dann läge der Quellbaum ohnehin vorn in
# sys.path, und selbst eine kaputte Installation sähe grün aus. Ein Import, der nur
# gelingt, weil man im richtigen Verzeichnis stand, sagt nichts über den Dienst.
log "Kurztest (Import als Service-User) …"
( cd / && sudo -u "${SERVICE_USER}" "${APP_DIR}/.venv/bin/python" -c "
import sys, sluice.server
resolved = sluice.server.__file__
expected = '${APP_DIR}/sluice/'
if not resolved.startswith(expected):
    sys.exit(
        'Sluice wird nicht aus ${APP_DIR} geladen, sondern aus:\n'
        '  ' + resolved + '\n'
        'Damit gilt im Dienst anderer Code als der hier ausgelieferte. Übrig gebliebene\n'
        'Installation entfernen und dieses Skript erneut laufen lassen.'
    )
print('ok —', resolved)" )

# Und jetzt das, was systemd wirklich tut: das Konsolen-Skript AUSFÜHREN. Der Importtest
# oben läuft über den Interpreter und übergeht damit die Shebang-Zeile — genau die Stelle,
# an der ein von woanders kopiertes venv scheitert. Ein `test -x` genügt hier ebenfalls
# nicht: es prüft nur die Bits des Skripts, nie den Interpreter dahinter.
log "Kurztest (uvicorn ausführen als Service-User) …"
for u in "${SERVICE_USER}" $(for p in ${PROVIDERS}; do echo "sluice-gw-${p}"; done) ${NER_USERS}; do
    if ! sudo -u "${u}" "${APP_DIR}/.venv/bin/uvicorn" --version >/dev/null 2>&1; then
        warn "User '${u}' kann ${APP_DIR}/.venv/bin/uvicorn nicht ausführen."
        warn "  Shebang: $(head -1 "${APP_DIR}/.venv/bin/uvicorn" 2>/dev/null || echo '?')"
        warn "  Zeigt der auf ein anderes Verzeichnis als ${APP_DIR}/.venv, ist das venv"
        warn "  von einer anderen Maschine — dann '${APP_DIR}/.venv' löschen und dieses"
        warn "  Skript erneut laufen lassen. Sonst: namei -l auf den Shebang-Pfad."
        exit 1
    fi
done
log "uvicorn ist für alle Service-User ausführbar."

if [[ "${WITH_NER}" == "1" ]]; then
    # Prüft Rechte und Modell-Abhängigkeiten des NER-Users. Das Modell selbst lädt hier
    # NICHT — `app` ist eine Factory, geladen wird erst beim Dienststart (fail-closed).
    log "Kurztest NER (Import als ${NER_USER}) …"
    sudo -u "${NER_USER}" "${APP_DIR}/.venv/bin/python" \
        -c "import sluice.ner.service, gliner; print('ok')"
fi

# --- 7. Nächste Schritte -----------------------------------------------------
GW_UNITS="$(for p in ${PROVIDERS}; do printf 'sluice-gateway@%s ' "${p}"; done)"

if [[ "${WITH_NER}" == "1" ]]; then
    NER_STEP="$(cat <<EOF
  3. NER-Dienst konfigurieren (§7.5 — nur für Profile mit mode = "pii_ner"):
       sudoedit ${CFG_DIR}/ner.env
     Zwei Dinge sind dort noch OFFEN und keine Formalie (docs/NER-SERVICE.md):
       a) SLUICE_NER_MODEL ist nicht vorentschieden — mindestens zwei Kandidaten mit
          scripts/eval_ner.py gegeneinander evaluieren, deutsche Abdeckung ist Pflicht.
       b) SLUICE_NER_REVISION setzen (Commit-Hash) — ohne ihn ist die
          Anonymisierungs-Identität (§5.4) nicht belastbar.
     CPU oder GPU erst messen, nicht raten:
       ${APP_DIR}/.venv/bin/python ${APP_DIR}/scripts/probe_ner_hardware.py --model <repo>
EOF
)"
    # Führendes \n in der Variablen statt einer eigenen Zeile in der Vorlage: sonst
    # bliebe im Nicht-NER-Fall je eine Leerzeile stehen.
    NER_START=$'\n       systemctl enable --now sluice-ner    # startet erst, wenn das Modell geladen ist'
    NER_CHECK=$'\n       curl -s http://127.0.0.1:17900/v1/info   # Modellidentität für die Profilverankerung'
    NER_FW=$'\n     NER-Port 17900 nur von der Kern-IP — über ihn geht ROHTEXT (§6).'
else
    NER_STEP="  3. (NER-Dienst nicht installiert — für Profile mit mode = \"pii_ner\":
     SLUICE_WITH_NER=1 bash deploy/bootstrap.sh, siehe docs/DEPLOY.md §5.2)"
    NER_START=""
    NER_CHECK=""
    NER_FW=""
fi

cat <<EOF

$(log "Fertig — Dienste sind installiert, aber bewusst NICHT gestartet.")
Nächste Schritte (docs/DEPLOY.md §4–§7):
  1. Profile pflegen — ohne passendes Profil blockiert Sluice jeden Egress (§4.3):
       sudoedit ${CFG_DIR}/profiles.toml
     Feld-Referenz inkl. Fallstricken: docs/PROFILES.md
  2. Je Gateway den EINEN Provider-Key eintragen (§5.1, Keys nur hier — nie im
     Kern, nie im Profil-TOML, nie im Repo):
$(for p in ${PROVIDERS}; do printf '       sudoedit %s/gateway-%s.env\n' "${CFG_DIR}" "${p}"; done)
${NER_STEP}
  4. Gateway-URLs des Kerns prüfen (§7.3 — Keys gehören NICHT in diese Datei):
       sudoedit ${CFG_DIR}/sluice.env
  5. Dienste starten (Gateways zuerst — der Kern ist ohne sie fail-closed):
       systemctl enable --now ${GW_UNITS}${NER_START}
       systemctl enable --now sluice${NER_CHECK}
  6. Firewall: :8000 nur aus dem Konsumenten-Netz, Gateway-Ports 17890–17893 nur
     von der Kern-IP, ausgehend :443 nur zu den vier Provider-Hosts (§6).${NER_FW}
  7. Abnahme 7.1–7.5 aus docs/DEPLOY.md durchlaufen, erst danach Konsumenten
     auf diese VM zeigen lassen.
EOF
