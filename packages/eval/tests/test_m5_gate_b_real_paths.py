"""C1: Gate B must exercise real paths and fail on real V1 regressions.

The corrective review found the original Gate B tautological: the expected
metrics were hard-coded as the produced result and the adapters never ran the
real V1 semantic path or the sealed legacy reader. These tests prove:

- Gate B invokes the real ``catalyst_agents.graph.run_v1_graph`` and the
  repo-owned sealed ``baseline.adapter.read_legacy_run_artifacts`` reader;
- Gate B derives metrics from actual run artifacts;
- Gate B FAILS when the real V1 path returns a bad citation, identity,
  lineage, ticker/cutoff, or secret leakage;
- no quality/comparability claim is made (comparability_declared=false).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from catalyst_eval.baseline.gates import (
    GateFailure,
    semantic_ontology_regression_gate,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_DIR = REPO_ROOT / "packages/eval/golden_set"


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)


def _gate(tmp_path: Path, **kwargs) -> dict:
    return semantic_ontology_regression_gate(
        repo_root=REPO_ROOT,
        golden_dir=GOLDEN_DIR,
        out_path=tmp_path / "gate_b.json",
        **kwargs,
    )


def test_gate_b_invokes_real_v1_graph_and_sealed_legacy_reader(tmp_path, monkeypatch) -> None:
    import catalyst_agents.graph as graph_module
    import catalyst_eval.baseline.adapter as adapter_module

    v1_calls = []
    real_run_v1_graph = graph_module.run_v1_graph

    def spy_v1(**kwargs):
        v1_calls.append(kwargs.get("run_id"))
        return real_run_v1_graph(**kwargs)

    monkeypatch.setattr(graph_module, "run_v1_graph", spy_v1)

    legacy_calls = []
    real_reader = adapter_module.read_legacy_run_artifacts

    def spy_reader(db_path, run_id):
        legacy_calls.append(run_id)
        return real_reader(db_path, run_id)

    monkeypatch.setattr(adapter_module, "read_legacy_run_artifacts", spy_reader)

    payload = _gate(tmp_path)
    assert payload["exit_status"] == 0
    assert len(v1_calls) == 10
    assert len(legacy_calls) == 10
    assert payload["comparability_declared"] is False


def _real_run_for_case(case_id: str) -> dict:
    """Run the real V1 fixture path for one approved case and return the
    raw artifact surface the gate derives metrics from."""
    from catalyst_eval.baseline.gates import v1_fixture_run

    from catalyst_eval.post_import.case_pack import build_smoke_case_pack

    cases = build_smoke_case_pack(GOLDEN_DIR)
    case = next(c for c in cases if c.case_id == case_id)
    return v1_fixture_run(case)


from catalyst_eval.baseline.gates import v1_fixture_run as real_v1_fixture_run


def _mutated_runner(mutate_fn):
    """Build a runner that runs the real V1 path per case and then mutates the
    artifact surface, proving the gate derives metrics from actual results."""

    def runner(case):
        run = real_v1_fixture_run(case)
        return mutate_fn(run, case)

    return runner


def test_gate_b_fails_when_v1_path_emits_bad_citation(tmp_path) -> None:
    def mutate(run, case):
        run["answer_text"] = run["answer_text"] + " (ghost)"
        return run

    with pytest.raises(GateFailure, match="citation"):
        _gate(tmp_path, v1_runner=_mutated_runner(mutate))


def test_gate_b_fails_when_v1_path_breaks_runtime_identity(tmp_path) -> None:
    def mutate(run, case):
        run["bound_runtime_identity"] = "b" * 64  # no longer matches injected identity
        return run

    with pytest.raises(GateFailure, match="runtime"):
        _gate(tmp_path, v1_runner=_mutated_runner(mutate))


def test_gate_b_fails_when_v1_path_breaks_claim_lineage(tmp_path) -> None:
    def mutate(run, case):
        plan = run["validated_claim_plan"]
        claims = tuple(
            claim.model_copy(update={"support_evidence_ids": ("ghost",)})
            if claim.role.value == "PRIMARY"
            else claim
            for claim in plan.claims
        )
        run["validated_claim_plan"] = plan.model_copy(update={"claims": claims})
        return run

    with pytest.raises(GateFailure, match="lineage"):
        _gate(tmp_path, v1_runner=_mutated_runner(mutate))


def test_gate_b_fails_when_v1_path_breaks_ticker_cutoff(tmp_path) -> None:
    def mutate(run, case):
        items = [{**item, "ticker": "OTHER"} for item in run["evidence_items"]]
        run["evidence_items"] = items
        return run

    with pytest.raises(GateFailure, match="ticker|cutoff"):
        _gate(tmp_path, v1_runner=_mutated_runner(mutate))


def test_gate_b_fails_when_v1_path_breaks_cutoff(tmp_path) -> None:
    def mutate(run, case):
        late = datetime.fromisoformat("2030-01-01T00:00:00+00:00")
        items = [{**item, "eligible_at": late} for item in run["evidence_items"]]
        run["evidence_items"] = items
        return run

    with pytest.raises(GateFailure, match="ticker|cutoff"):
        _gate(tmp_path, v1_runner=_mutated_runner(mutate))


def test_gate_b_fails_when_v1_path_leaks_secret(tmp_path) -> None:
    def mutate(run, case):
        run["answer_text"] = run["answer_text"] + " api_key=sk-test-secret-123456"
        return run

    with pytest.raises(GateFailure, match="leak"):
        _gate(tmp_path, v1_runner=_mutated_runner(mutate))


def test_gate_b_context_pack_identity_is_derived_not_hardcoded(tmp_path) -> None:
    def mutate(run, case):
        run["context_pack_sha256"] = "not-a-hex-digest"
        return run

    with pytest.raises(GateFailure, match="context"):
        _gate(tmp_path, v1_runner=_mutated_runner(mutate))
