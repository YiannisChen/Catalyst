from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "run_experiments.py"
SPEC = importlib.util.spec_from_file_location("run_experiments", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MODULE)
run_e2_experiment = MODULE.run_e2_experiment


def test_run_e2_experiment_requires_retrieval_dependencies():
    with pytest.raises(ValueError) as excinfo:
        run_e2_experiment(golden_set=[], llm=object(), table=None, embedding_fn=None, reranker=None)

    message = str(excinfo.value)
    assert "E2 requires a populated LanceDB table" in message
    assert "embedding_fn" in message
    assert "reranker" in message
    assert "current script only supports e2" in message.lower()
