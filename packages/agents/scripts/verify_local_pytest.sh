#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_PYTHON="${VENV_PYTHON:-/Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python}"

cd "${PACKAGE_DIR}"
"${VENV_PYTHON}" -m pytest tests/ -q --tb=short
