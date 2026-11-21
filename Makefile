PYTHON ?= python3
PIP ?= $(PYTHON) -m pip

.PHONY: install test lint web dev-mock contract

install:
	$(PIP) install -r requirements-dev.txt

TEST_DIRS = packages/data/tests packages/agents/tests packages/api/tests deploy/monitor/tests deploy/api/tests deploy/ops/tests

# Each package has its own conftest, so each suite runs in its own pytest process (as in CI).
test:
	@status=0; for tests in $(TEST_DIRS); do \
		HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONPATH=packages/data:packages/agents:packages/api:scripts \
		$(PYTHON) -m pytest "$$tests" -q --disable-warnings || status=1; \
	done; exit $$status

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
