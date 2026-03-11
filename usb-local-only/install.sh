#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${HOME}/.local/share/mint-dictate-local"
CONFIG_DIR="${HOME}/.config/mint-dictate-local"
SERVICE_DIR="${HOME}/.config/systemd/user"
SERVICE_NAME="mint-dictate-local.service"

mkdir -p "${INSTALL_DIR}"
mkdir -p "${CONFIG_DIR}"
mkdir -p "${SERVICE_DIR}"

install -m 0644 "${SCRIPT_DIR}/mint_dictate.py" "${INSTALL_DIR}/mint_dictate.py"
install -m 0644 "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/requirements.txt"
install -m 0644 "${SCRIPT_DIR}/LICENSE" "${INSTALL_DIR}/LICENSE"

if [[ ! -f "${CONFIG_DIR}/config.json" ]]; then
  install -m 0644 "${SCRIPT_DIR}/config.json" "${CONFIG_DIR}/config.json"
fi

python3 -m venv "${INSTALL_DIR}/.venv"
"${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

python3 - <<PY
from pathlib import Path

service_template = Path("${SCRIPT_DIR}/mint-dictate-local.service").read_text(encoding="utf-8")
service_text = service_template.replace("__INSTALL_DIR__", "${INSTALL_DIR}")
Path("${SERVICE_DIR}/${SERVICE_NAME}").write_text(service_text, encoding="utf-8")
PY

systemctl --user daemon-reload
systemctl --user enable --now "${SERVICE_NAME}"

"${INSTALL_DIR}/.venv/bin/python" -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8'); print('small model ready')"
"${INSTALL_DIR}/.venv/bin/python" -c "from faster_whisper import WhisperModel; WhisperModel('medium', device='cpu', compute_type='int8'); print('medium model ready')"
"${INSTALL_DIR}/.venv/bin/python" -c "from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', device='cpu', compute_type='int8'); print('large-v3-turbo model ready')"

echo "Installed to ${INSTALL_DIR}"
echo "Config file: ${CONFIG_DIR}/config.json"
echo "Service: ${SERVICE_NAME}"
