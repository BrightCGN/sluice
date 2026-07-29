#!/usr/bin/env bash
#
# Sluice — Provisionierung der Sluice-VM (ID 8740, 192.168.87.40).
#
# Legt den kompletten Stack an: Kern-Service (sluice.service) + je ein eigenständiges
# Provider-Gateway (sluice-gateway@<provider>), jedes unter eigenem System-User
# (Rev. 8, User-Isolation). Idempotent gedacht: mehrfaches Ausführen soll nichts
# kaputt machen — bestehende Konfiguration in /etc/sluice wird NIE überschrieben.
#
# Voraussetzung: Debian 12 / Ubuntu 24.04 mit root/sudo, Netz, Python >= 3.11.
# Aufruf (als root):   bash deploy/bootstrap.sh
#
# Fail-closed by design (§4.3): das Skript startet die Dienste NICHT automatisch.
# Ohne ausgefüllte profiles.toml und Gateway-Keys wäre das nur ein Service, der
# alles blockiert — die Freigabe ist bewusst ein manueller, letzter Schritt.
#
# Bezug: docs/DEPLOY.md (Schritt 1–5), docs/SLUICE-BOUNDARY-SPEC.md (§4, §5, §7.3).

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

log()  { printf '\033[1;34m[bootstrap]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[bootstrap]\033[0m %s\n' "$*" >&2; }

if [[ "${EUID}" -ne 0 ]]; then
    echo "Bitte als root ausführen (sudo bash deploy/bootstrap.sh)." >&2
    exit 1
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
for u in "${SERVICE_USER}" $(for p in ${PROVIDERS}; do echo "sluice-gw-${p}"; done); do
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
    rsync -a --delete \
        --exclude '.git' --exclude '__pycache__' --exclude '.venv' \
        --exclude '.pytest_cache' \
        "${REPO_DIR}/" "${APP_DIR}/"
fi

# --- 4. venv + Installation --------------------------------------------------
if [[ ! -d "${APP_DIR}/.venv" ]]; then
    log "Erzeuge venv …"
    python3 -m venv "${APP_DIR}/.venv"
fi
log "Installiere Sluice …"
"${APP_DIR}/.venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/.venv/bin/pip" install --quiet "${APP_DIR}"

# Kern-User besitzt den Baum; die Gateway-User teilen sich NUR den Code (o+rX),
# sonst nichts (DEPLOY.md §3). Provider-Keys liegen in /etc/sluice, nie hier.
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"
chmod -R a+rX "${APP_DIR}"

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
fi
chown root:root "${CFG_DIR}/sluice.env"
chmod 600 "${CFG_DIR}/sluice.env"

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

# --- 6. systemd-Units --------------------------------------------------------
log "Installiere systemd-Units …"
cp "${APP_DIR}/deploy/sluice.service" /etc/systemd/system/
cp "${APP_DIR}/deploy/sluice-gateway@.service" /etc/systemd/system/
if [[ "${BIND_HOST}" != "${DEFAULT_HOST}" ]]; then
    log "Passe Bind-Adresse der installierten Unit auf ${BIND_HOST} an …"
    sed -i "s/${DEFAULT_HOST}/${BIND_HOST}/g" /etc/systemd/system/sluice.service
fi
chmod 644 /etc/systemd/system/sluice.service /etc/systemd/system/sluice-gateway@.service
systemctl daemon-reload

# Die Unit bindet an eine feste IP — fehlt sie auf dieser Maschine, scheitert der
# Start erst später mit "Cannot assign requested address". Lieber jetzt sagen.
if ! ip -o addr show 2>/dev/null | grep -qw "${BIND_HOST}"; then
    warn "Adresse ${BIND_HOST} ist auf dieser Maschine nicht konfiguriert —"
    warn "  sluice.service kann so nicht binden. Statische IP setzen oder"
    warn "  Bootstrap mit SLUICE_BIND_HOST=<ip> erneut laufen lassen."
fi

# Importtest als Service-User: findet kaputte venvs/Rechte vor dem ersten Start.
log "Kurztest (Import als Service-User) …"
sudo -u "${SERVICE_USER}" "${APP_DIR}/.venv/bin/python" -c "import sluice.server; print('ok')"

# --- 7. Nächste Schritte -----------------------------------------------------
GW_UNITS="$(for p in ${PROVIDERS}; do printf 'sluice-gateway@%s ' "${p}"; done)"

cat <<EOF

$(log "Fertig — Dienste sind installiert, aber bewusst NICHT gestartet.")
Nächste Schritte (docs/DEPLOY.md §4–§7):
  1. Profile pflegen — ohne passendes Profil blockiert Sluice jeden Egress (§4.3):
       sudoedit ${CFG_DIR}/profiles.toml
     Feld-Referenz inkl. Fallstricken: docs/PROFILES.md
  2. Je Gateway den EINEN Provider-Key eintragen (§5.1, Keys nur hier — nie im
     Kern, nie im Profil-TOML, nie im Repo):
$(for p in ${PROVIDERS}; do printf '       sudoedit %s/gateway-%s.env\n' "${CFG_DIR}" "${p}"; done)
  3. Gateway-URLs des Kerns prüfen (§7.3 — Keys gehören NICHT in diese Datei):
       sudoedit ${CFG_DIR}/sluice.env
  4. Dienste starten (Gateways zuerst — der Kern ist ohne sie fail-closed):
       systemctl enable --now ${GW_UNITS}
       systemctl enable --now sluice
  5. Firewall: :8000 nur aus dem Konsumenten-Netz, Gateway-Ports 17890–17893 nur
     von der Kern-IP, ausgehend :443 nur zu den vier Provider-Hosts (§6).
  6. Abnahme 7.1–7.5 aus docs/DEPLOY.md durchlaufen, erst danach Konsumenten
     auf diese VM zeigen lassen.
EOF
