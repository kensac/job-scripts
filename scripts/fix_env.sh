#!/usr/bin/env bash
set -euo pipefail

VENV_NAME="job-scripts"
PY_VERSION="3.13.5"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Removing existing virtualenv '${VENV_NAME}'..."
pyenv uninstall -f "${VENV_NAME}" 2>/dev/null || true

# Check if base Python is working
if ! pyenv exec python --version 2>/dev/null; then
    echo "Base Python ${PY_VERSION} is broken, reinstalling..."
    pyenv uninstall -f "${PY_VERSION}" 2>/dev/null || true
    pyenv install "${PY_VERSION}"
fi

echo "Creating virtualenv '${VENV_NAME}' with Python ${PY_VERSION}..."
pyenv virtualenv "${PY_VERSION}" "${VENV_NAME}"

echo "Installing dependencies..."
PYENV_VERSION="${VENV_NAME}" pyenv exec pip install -r "${SCRIPT_DIR}/../requirements.txt"

echo "Done. Virtualenv '${VENV_NAME}' is ready."
