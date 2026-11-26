#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_USER="algo"
SERVICE_GROUP="algo"
APP_DIR="/opt/nsebot"
DATA_DIR="/var/lib/nsebot"
ENV_DIR="/etc/algo"
ENV_FILE="${ENV_DIR}/env"
SYSTEMD_DIR="/etc/systemd/system"
UNITS=("algo-api.service" "algo-engine.service")

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

if [[ "${EUID}" -ne 0 ]]; then
    fail "Run this script with sudo: sudo bash deploy/setup-ec2.sh"
fi

command -v systemctl >/dev/null 2>&1 || fail "systemd/systemctl is required."
command -v tar >/dev/null 2>&1 || fail "tar is required."
[[ -f "${SOURCE_DIR}/pyproject.toml" ]] || fail "Run this script from a project source checkout."
[[ -f "${SOURCE_DIR}/deploy/algo-api.service" ]] || fail "API service unit is missing."
[[ -f "${SOURCE_DIR}/deploy/algo-engine.service" ]] || fail "Engine service unit is missing."

PYTHON=""
for candidate in python3.12 python3.13 python3.14 python3; do
    if command -v "${candidate}" >/dev/null 2>&1 \
        && "${candidate}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
        PYTHON="$(command -v "${candidate}")"
        break
    fi
done
[[ -n "${PYTHON}" ]] || fail "Python 3.12 or newer is required. Install it, then rerun this script."

if [[ -L "${APP_DIR}" ]]; then
    fail "${APP_DIR} is a symlink; refusing to deploy through it."
fi

if ! getent group "${SERVICE_GROUP}" >/dev/null 2>&1; then
    groupadd --system "${SERVICE_GROUP}"
fi
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --gid "${SERVICE_GROUP}" --home-dir "${DATA_DIR}" \
        --no-create-home --shell /sbin/nologin "${SERVICE_USER}"
elif ! id -Gn "${SERVICE_USER}" | tr ' ' '\n' | grep -Fxq "${SERVICE_GROUP}"; then
    usermod --append --groups "${SERVICE_GROUP}" "${SERVICE_USER}"
fi

install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0700 "${DATA_DIR}"
chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${DATA_DIR}"
install -d -o root -g "${SERVICE_GROUP}" -m 0750 "${APP_DIR}"
install -d -o root -g "${SERVICE_GROUP}" -m 0750 "${ENV_DIR}"

if [[ ! -e "${ENV_FILE}" ]]; then
    ui_password="$("${PYTHON}" -c 'import secrets; print(secrets.token_urlsafe(32))')"
    umask 077
    cat > "${ENV_FILE}" <<EOF
DATA_DIR=${DATA_DIR}
REPO_DATA_DIR=${APP_DIR}/data
LIVE_TRADING=false
DATABASE_BACKEND=dynamodb
DYNAMODB_TABLE=nsebot
AWS_REGION=ap-south-1
UI_PASSWORD=${ui_password}
KITE_API_KEY=
KITE_API_SECRET=
XAI_API_KEY=
ALERT_WEBHOOK=
EOF
    chown root:"${SERVICE_GROUP}" "${ENV_FILE}"
    chmod 0640 "${ENV_FILE}"
    printf 'Created protected environment file: %s\n' "${ENV_FILE}"
else
    chown root:"${SERVICE_GROUP}" "${ENV_FILE}"
    chmod 0640 "${ENV_FILE}"
fi

require_setting() {
    local key="$1"
    local value="$2"
    if ! grep -Eq "^${key}=${value}$" "${ENV_FILE}"; then
        fail "${ENV_FILE} must contain exactly ${key}=${value}; review it and rerun."
    fi
}

require_setting "DATA_DIR" "${DATA_DIR}"
require_setting "REPO_DATA_DIR" "${APP_DIR}/data"
require_setting "LIVE_TRADING" "false"
if ! grep -Eq '^UI_PASSWORD=[^[:space:]#][^#]*$' "${ENV_FILE}" \
    || grep -Eq '^UI_PASSWORD=REPLACE_' "${ENV_FILE}"; then
    fail "Set a non-empty UI_PASSWORD in ${ENV_FILE}, then rerun."
fi

read_setting() {
    local key="$1"
    sed -n "s/^${key}=//p" "${ENV_FILE}" | tail -n 1
}

DATABASE_BACKEND="$(read_setting DATABASE_BACKEND)"
case "${DATABASE_BACKEND}" in
    sqlite)
        ;;
    dynamodb)
        DYNAMODB_TABLE="$(read_setting DYNAMODB_TABLE)"
        AWS_REGION="$(read_setting AWS_REGION)"
        [[ -n "${DYNAMODB_TABLE}" ]] || fail "Set DYNAMODB_TABLE in ${ENV_FILE}."
        [[ -n "${AWS_REGION}" ]] || fail "Set AWS_REGION in ${ENV_FILE}, then rerun setup."
        ;;
    *)
        fail "DATABASE_BACKEND in ${ENV_FILE} must be sqlite or dynamodb."
        ;;
esac

for unit in "${UNITS[@]}"; do
    if systemctl is-active --quiet "${unit}"; then
        systemctl stop "${unit}"
    fi
done

tar -C "${SOURCE_DIR}" \
    --exclude='./.git' \
    --exclude='./.venv' \
    --exclude='./venv' \
    --exclude='./var' \
    --exclude='./__pycache__' \
    --exclude='*/__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.egg-info' \
    --exclude='./.env' \
    --exclude='./ssh connect.txt' \
    --exclude='*.pem' \
    --exclude='*.key' \
    -cf - . | tar -C "${APP_DIR}" -xf -

if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
    "${PYTHON}" -m venv "${APP_DIR}/.venv"
fi
"${APP_DIR}/.venv/bin/python" -c \
    'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
    || fail "The existing ${APP_DIR}/.venv uses Python older than 3.12; replace it before deploying."
"${APP_DIR}/.venv/bin/python" -m pip install "${APP_DIR}"

if [[ "${DATABASE_BACKEND}" == "dynamodb" ]]; then
    AWS_REGION="${AWS_REGION}" DYNAMODB_TABLE="${DYNAMODB_TABLE}" \
        "${APP_DIR}/.venv/bin/nsebot-dynamodb-init"
fi

chown -R root:"${SERVICE_GROUP}" "${APP_DIR}"
chmod -R o-rwx "${APP_DIR}"
find "${APP_DIR}" -type d -exec chmod g+rx {} +
find "${APP_DIR}" -type f -exec chmod g+r {} +

install -o root -g root -m 0644 "${SOURCE_DIR}/deploy/algo-api.service" \
    "${SYSTEMD_DIR}/algo-api.service"
install -o root -g root -m 0644 "${SOURCE_DIR}/deploy/algo-engine.service" \
    "${SYSTEMD_DIR}/algo-engine.service"

systemctl daemon-reload
for unit in "${UNITS[@]}"; do
    systemctl enable "${unit}"
    if systemctl is-active --quiet "${unit}"; then
        systemctl restart "${unit}"
    else
        systemctl start "${unit}"
    fi
done

printf '\nDeployment complete. Service status:\n'
systemctl --no-pager --full status "${UNITS[@]}"
printf '\nThe API listens on 127.0.0.1:8000. Use an SSH tunnel to access it remotely.\n'
printf 'If this script created the UI password, retrieve it with:\n'
printf '  sudo grep "^UI_PASSWORD=" %s\n' "${ENV_FILE}"
printf '\nRuntime data and credentials are kept in %s and are not overwritten by deployment.\n' "${DATA_DIR}"
