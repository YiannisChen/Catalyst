#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
export PYTHONPATH=packages/data:packages/agents:packages/api:scripts
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

for tests in \
    packages/data/tests \
    packages/agents/tests \
    packages/api/tests \
    deploy/monitor/tests \
    deploy/api/tests
do
    if find "$tests" -type f \( -name 'test_*.py' -o -name '*_test.py' \) | grep -q .; then
        .venv/bin/python -m pytest "$tests" -q --disable-warnings
    else
        printf 'No test files in %s\n' "$tests"
    fi
done

.venv/bin/python scripts/check_ruff.py
.venv/bin/lint-imports --config pyproject.toml --no-logo
git diff --check
