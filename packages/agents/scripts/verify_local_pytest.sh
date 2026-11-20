#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_PYTHON="${VENV_PYTHON:-${PACKAGE_DIR}/../data/.venv/bin/python}"

cd "${PACKAGE_DIR}"
"${VENV_PYTHON}" -m pytest tests/ -q --tb=short
