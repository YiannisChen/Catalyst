"""
S1 Phase 7: Three-Arm Experiment Tests.

Split into:
  (a) Eval-only fidelity tests — run always, use COMMITTED fixture artifact
      from tests/fixtures/.  Test byte-identity + structural template through
      the persisted node_artifacts boundary.
  (b) Cross-package integration tests — @pytest.mark.integration with
      pytest.importorskip("catalyst_agents").  Test Arm C miner instrumentation
      and subset fidelity check end-to-end.

Arm-B semantics (CORRECTED):
  Arm B consumes arm_b_evidence (pre-Critic top-8 from miner node).
  judge_evidence is a SECONDARY subset fidelity check.
  B→C delta = Critic filtering + annotations + guards + Validator = MCJ structural value.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

# ===========================================================================
# Committed fixture
# ===========================================================================

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

def _load_arm_b_fixture() -> dict:
    """Load the committed arm_b_evidence fixture."""
    path = _FIXTURE_DIR / "arm_b_evidence_fixture.json"
    assert path.exists(), f"Committed fixture missing: {path}"
    return json.loads(path.read_text())


# ===========================================================================
# (a) Eval-only fidelity tests — always run
# ===========================================================================

class TestArmBFidelityEvalOnly:
    """Byte-identity + structural template tests using committed fixture.

    These tests do NOT import catalyst_agents — they verify that Arm B's
    independent reconstruction matches the committed fixture.
    """

    def test_reconstructed_block_matches_committed_sha(self):
        """Arm B reconstructs evidence block → SHA matches committed fixture."""
        from catalyst_eval.harness.three_arm import _reconstruct_evidence_block

        fixture = _load_arm_b_fixture()
        per_asset = fixture["per_asset"]
        expected_sha = fixture["sha256"]

        # Arm B reconstructs independently
        block = _reconstruct_evidence_block(per_asset)
        arm_b_sha = hashlib.sha256(block.encode("utf-8")).hexdigest()

        assert arm_b_sha == expected_sha, (
            f"Arm B SHA {arm_b_sha[:16]}... != committed fixture {expected_sha[:16]}..."
        )

    def test_structural_template_no_critic_fields(self):
        """Evidence block must NOT contain Critic metadata fields."""
        from catalyst_eval.harness.three_arm import _reconstruct_evidence_block

        fixture = _load_arm_b_fixture()
        block = _reconstruct_evidence_block(fixture["per_asset"])

        assert "Relevance:" not in block
        assert "Category:" not in block
        assert "Critic reasoning:" not in block
        assert "### Evidence " in block
        assert "\n\n---\n\n" in block

    def test_arm_b_consumes_arm_b_evidence(self):
        """Arm B accepts arm_b_evidence artifact and produces result."""
        from catalyst_eval.harness.three_arm import arm_b_same_evidence
        from catalyst_eval.schema.golden_event import GoldenEvent

        fixture = _load_arm_b_fixture()
        golden = GoldenEvent(
            id="g001", ticker="TSLA", trade_date="2025-01-02",
            price_move_pct=-6.08, should_refuse=False, causes=[],
        )

        def fixture_judge(prompt: str) -> dict:
            assert "### Evidence " in prompt, "Arm B prompt must contain evidence"
            return {"causes": [], "summary_md": ""}

        result = arm_b_same_evidence(golden, fixture, judge_llm=fixture_judge)
        assert result.evidence_block_sha256 == fixture["sha256"]

    def test_arm_b_answers_when_artifact_has_evidence_even_if_critic_would_refuse(self):
        """Arm B MUST attempt an answer when arm_b_evidence exists,
        even though a Critic might later refuse on this case.
        This is what makes refusal_correctness a real measurement.
        """
        from catalyst_eval.harness.three_arm import arm_b_same_evidence
        from catalyst_eval.schema.golden_event import GoldenEvent

        fixture = _load_arm_b_fixture()
        golden = GoldenEvent(
            id="h006", ticker="TSLA", trade_date="2025-10-22",
            price_move_pct=0.0, should_refuse=True, causes=[],
        )

        def fixture_judge(prompt: str) -> dict:
            # Arm B attempts to answer despite should_refuse
            return {
                "causes": [
                    {"text": "Market noise", "category": "technical",
                     "confidence": 0.3, "evidence_ids": [], "direction": "neutral"}
                ],
                "summary_md": "Minimal movement.",
            }

        result = arm_b_same_evidence(golden, fixture, judge_llm=fixture_judge)
        # Arm B produced causes even on a should_refuse case
        assert len(result.causes) > 0, (
            "Arm B must attempt an answer on pre-Critic evidence, "
            "even for should_refuse cases — otherwise refusal comparison is tautological"
        )


# ===========================================================================
# Arm A: Closed-book baseline
# ===========================================================================

class TestArmAClosedBook:
    def test_arm_a_prompt_has_no_evidence_sections(self):
        from catalyst_eval.harness.three_arm import arm_a_closed_book
        from catalyst_eval.schema.golden_event import GoldenEvent

        golden = GoldenEvent(
            id="g001", ticker="TSLA", trade_date="2025-01-02",
            price_move_pct=-6.08, should_refuse=False, causes=[],
        )
        captured = []

        def capture(prompt: str) -> dict:
            captured.append(prompt)
            return {"causes": [], "summary_md": ""}

        arm_a_closed_book(golden, judge_llm=capture)
        prompt = captured[0]
        assert "NO evidence" in prompt or "no evidence" in prompt.lower() or "with no evidence" in prompt.lower()
        assert "### Evidence " not in prompt, "Arm A must NOT contain evidence blocks"


# ===========================================================================
# Arm C: Full MCJ
# ===========================================================================

class TestArmCFullMCJ:
    def test_arm_c_delegates_to_catalyst(self):
        from catalyst_eval.harness.three_arm import arm_c_full_mcj
        from catalyst_eval.schema.golden_event import GoldenEvent
        from catalyst_eval.schema.result import AttributionResult

        golden = GoldenEvent(
            id="g001", ticker="TSLA", trade_date="2025-01-02",
            price_move_pct=-6.08, should_refuse=False, causes=[],
        )
        calls = []

        def catalyst_predict(ticker: str, trade_date: str) -> AttributionResult:
            calls.append((ticker, trade_date))
            return AttributionResult(ticker=ticker, trade_date=trade_date, causes=[], summary="")

        arm_c_full_mcj(golden, catalyst_predict=catalyst_predict)
        assert calls == [("TSLA", "2025-01-02")]


# ===========================================================================
# (b) Cross-package integration tests
# ===========================================================================

@pytest.mark.integration
class TestArmBSubsetIntegration:
    """Cross-package tests that verify miner instrumentation and subset fidelity.

    These import catalyst_agents (requires editable install in main venv).
    """

    def test_arm_b_evidence_in_miner(self):
        """Verify _build_arm_b_evidence exists in miner module."""
        catalyst_agents = pytest.importorskip("catalyst_agents")
        from catalyst_agents.nodes.miner import _build_arm_b_evidence

        result = _build_arm_b_evidence([])
        assert result == {"per_asset": {}, "sha256": ""}

        # With real chunks
        chunks = [
            {"asset_id": "test:1::l1", "content_md": "## Test\nContent A"},
            {"asset_id": "test:2::l1", "content_md": "## Test\nContent B"},
        ]
        result = _build_arm_b_evidence(chunks)
        assert len(result["per_asset"]) == 2
        assert result["sha256"]
        assert len(result["sha256"]) == 64

    def test_arm_b_evidence_registered_in_projection(self):
        """Verify arm_b_evidence is in miner's artifact types."""
        pytest.importorskip("catalyst_agents")
        from catalyst_agents.trace.projection import _NODE_ARTIFACT_TYPES

        assert "arm_b_evidence" in _NODE_ARTIFACT_TYPES["miner"], (
            "arm_b_evidence must be registered for miner node"
        )

    def test_subset_fidelity_holds(self):
        """judge_evidence.per_asset ⊆ arm_b_evidence.per_asset with byte-equal values."""
        from catalyst_eval.harness.three_arm import _verify_subset_fidelity

        arm_b = {
            "per_asset": {
                "a:1": "content A",
                "a:2": "content B",
                "a:3": "content C",
            },
            "sha256": "abc",
        }
        judge = {
            "per_asset": {
                "a:1": "content A",
                "a:3": "content C",
            },
            "sha256": "def",
        }
        # Should not raise
        _verify_subset_fidelity(arm_b, judge)

    def test_subset_fidelity_raises_on_missing_asset(self):
        """judge_evidence referencing asset not in arm_b_evidence must raise."""
        from catalyst_eval.harness.three_arm import _verify_subset_fidelity

        arm_b = {"per_asset": {"a:1": "content A"}, "sha256": "abc"}
        judge = {"per_asset": {"a:1": "content A", "a:99": "missing"}, "sha256": "def"}

        with pytest.raises(AssertionError, match="not found in arm_b_evidence"):
            _verify_subset_fidelity(arm_b, judge)

    def test_subset_fidelity_raises_on_content_mismatch(self):
        """Different content for same asset_id must raise."""
        from catalyst_eval.harness.three_arm import _verify_subset_fidelity

        arm_b = {"per_asset": {"a:1": "content A"}, "sha256": "abc"}
        judge = {"per_asset": {"a:1": "DIFFERENT content"}, "sha256": "def"}

        with pytest.raises(AssertionError, match="different content"):
            _verify_subset_fidelity(arm_b, judge)
