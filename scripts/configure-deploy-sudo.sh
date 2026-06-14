#!/usr/bin/env bash
set -euo pipefail

RUNNER_USER="${1:-}"
WORKSPACE="${2:-}"
SUDOERS_FILE="/etc/sudoers.d/getoffline-actions"

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this setup script as root (for example, with sudo)." >&2
    exit 1
fi

if [[ -z "${RUNNER_USER}" || -z "${WORKSPACE}" ]]; then
    echo "Usage: $0 RUNNER_USER GITHUB_WORKSPACE" >&2
    exit 1
fi

if ! id "${RUNNER_USER}" >/dev/null 2>&1; then
    echo "Runner user '${RUNNER_USER}' does not exist." >&2
    exit 1
fi

WORKSPACE="$(realpath "${WORKSPACE}")"
DEPLOY_SCRIPT="${WORKSPACE}/scripts/deploy-local.sh"

if [[ ! -x "${DEPLOY_SCRIPT}" ]]; then
    echo "Executable deployment script not found at ${DEPLOY_SCRIPT}." >&2
    exit 1
fi

if [[ "${RUNNER_USER}" =~ [[:space:],:=\\] || "${DEPLOY_SCRIPT}" =~ [[:space:],:=\\] ]]; then
    echo "Runner user and workspace path must not contain sudoers special characters." >&2
    exit 1
fi

if ! command -v visudo >/dev/null 2>&1; then
    echo "visudo is required to configure passwordless deployment." >&2
    exit 1
fi

TEMP_FILE="$(mktemp)"
trap 'rm -f "${TEMP_FILE}"' EXIT

printf '%s ALL=(root) NOPASSWD: %s %s\n' \
    "${RUNNER_USER}" "${DEPLOY_SCRIPT}" "${WORKSPACE}" >"${TEMP_FILE}"
chmod 0440 "${TEMP_FILE}"
visudo --check --file="${TEMP_FILE}"
install -o root -g root -m 0440 "${TEMP_FILE}" "${SUDOERS_FILE}"

echo "Installed ${SUDOERS_FILE}:"
cat "${SUDOERS_FILE}"
echo
echo "Passwordless deployment is configured for ${RUNNER_USER}."
