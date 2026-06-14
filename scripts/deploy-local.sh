#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="${1:-$PWD}"
INSTALL_DIR="${GETOFFLINE_INSTALL_DIR:-/opt/getoffline}"
DATA_DIR="${GETOFFLINE_DATA_DIR:-/var/lib/getoffline}"
SERVICE_FILE="/etc/systemd/system/getoffline.service"
SERVICE_USER="${GETOFFLINE_SERVICE_USER:-jellyfin}"
SERVICE_GROUP="${GETOFFLINE_SERVICE_GROUP:-jellyfin}"

if [[ "${EUID}" -ne 0 ]]; then
    echo "This deployment script must run as root (for example, with sudo)." >&2
    exit 1
fi

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    echo "Service user '${SERVICE_USER}' does not exist." >&2
    exit 1
fi

SOURCE_DIR="$(realpath "${SOURCE_DIR}")"
if [[ ! -f "${SOURCE_DIR}/src/requirements.txt" ]]; then
    echo "GetOffline source tree not found at ${SOURCE_DIR}." >&2
    exit 1
fi

echo "Stopping GetOffline before updating application files..."
systemctl stop getoffline.service 2>/dev/null || true

install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${INSTALL_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${DATA_DIR}/downloads"

if [[ -d "${INSTALL_DIR}/downloads" && ! -L "${INSTALL_DIR}/downloads" ]]; then
    echo "Moving existing downloads into ${DATA_DIR}/downloads..."
    cp -a -n "${INSTALL_DIR}/downloads/." "${DATA_DIR}/downloads/"
    rm -rf "${INSTALL_DIR}/downloads"
fi

echo "Copying application files to ${INSTALL_DIR}..."
find "${INSTALL_DIR}" -mindepth 1 -maxdepth 1 \
    ! -name downloads \
    ! -name app.log \
    -exec rm -rf -- {} +
cp -a \
    "${SOURCE_DIR}/src" \
    "${SOURCE_DIR}/config.yml" \
    "${SOURCE_DIR}/README.md" \
    "${INSTALL_DIR}/"
ln -sfn "${DATA_DIR}/downloads" "${INSTALL_DIR}/downloads"

echo "Creating the deployment virtual environment..."
python3 -m venv "${INSTALL_DIR}/.venv"
"${INSTALL_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${INSTALL_DIR}/.venv/bin/python" -m pip install -r "${INSTALL_DIR}/src/requirements.txt"

chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${INSTALL_DIR}" "${DATA_DIR}"

echo "Installing and starting the systemd service..."
install -m 0644 "${SOURCE_DIR}/deploy/getoffline.service" "${SERVICE_FILE}"
systemctl daemon-reload
systemctl enable --now getoffline.service
systemctl --no-pager --full status getoffline.service
