PYTHON ?= python3
PIP ?= $(PYTHON) -m pip

.PHONY: install test lint web dev-mock contract

install:
	$(PIP) install -r requirements-dev.txt

test:
	HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONPATH=packages/data:packages/agents:packages/api:scripts $(PYTHON) -m pytest packages/data/tests packages/agents/tests packages/api/tests deploy/monitor/tests deploy/api/tests -q --disable-warnings

lint:
	$(PYTHON) scripts/check_ruff.py
	lint-imports --config pyproject.toml --no-logo

web:
	cd apps/web && npm run dev

dev-mock:
	cd apps/web && npm run dev:mock

contract:
	PYTHONPATH=packages/api:packages/agents:packages/data $(PYTHON) scripts/export_api_contract.py
	cd apps/web && npm run gen:api
