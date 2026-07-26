from __future__ import annotations

from attribution_fixtures import make_hypothesis


def test_output_status_values():
    from catalyst_agents.attribution.output_status import OutputStatus

    assert OutputStatus.SUFFICIENT == "SUFFICIENT"
    assert OutputStatus.PARTIAL == "PARTIAL"
    assert OutputStatus.ABSTAIN == "ABSTAIN"
    assert OutputStatus.SYSTEM_ERROR == "SYSTEM_ERROR"


def test_sufficient_requires_one_gate_passed():
    from catalyst_agents.attribution.output_status import determine_status

    status = determine_status([make_hypothesis(gate_passed=True, direct_support=True)], cutoff_violations=0, citation_all_resolve=True, coverage_degraded=False, context_quality_ok=True)
    assert status == "SUFFICIENT"


def test_partial_when_coverage_degraded():
    from catalyst_agents.attribution.output_status import determine_status

    status = determine_status([make_hypothesis(gate_passed=True, direct_support=True)], cutoff_violations=0, citation_all_resolve=True, coverage_degraded=True, context_quality_ok=True)
    assert status == "PARTIAL"


def test_abstain_when_no_gates_passed():
    from catalyst_agents.attribution.output_status import determine_status

    status = determine_status([], cutoff_violations=0, citation_all_resolve=True, coverage_degraded=False, context_quality_ok=True)
    assert status == "ABSTAIN"


def test_system_error_on_schema_failure():
    from catalyst_agents.attribution.output_status import determine_status

    status = determine_status(None, cutoff_violations=0, citation_all_resolve=True, coverage_degraded=False, context_quality_ok=True, error_occurred=True)
    assert status == "SYSTEM_ERROR"


def test_historical_insufficient_decodes_to_abstain():
    from catalyst_agents.attribution.output_status import decode_output_status

    assert decode_output_status("INSUFFICIENT") == "ABSTAIN"


def test_state_reexports_canonical_output_status():
    from catalyst_agents.attribution.output_status import OutputStatus as Canonical
    from catalyst_agents.state import OutputStatus

    assert OutputStatus is Canonical
    assert not hasattr(OutputStatus, "INSUFFICIENT")
