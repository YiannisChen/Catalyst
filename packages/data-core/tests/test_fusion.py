from __future__ import annotations

import pytest

from retrieval_model_fixtures import make_result, make_results


def test_rrf_formula():
    from catalyst_data.retrieval.fusion import compute_rrf

    assert compute_rrf(lexical_rank=1, dense_rank=5, k=60) == pytest.approx(
        1 / 61 + 1 / 65
    )


def test_rrf_fuses_top_20_each():
    from catalyst_data.retrieval.fusion import fuse

    lexical = make_results(20, prefix="lex")
    dense = make_results(20, prefix="den")
    fused = fuse(lexical, dense, k=60, output_k=20)
    assert len(fused) == 20
    assert len({item.chunk_id for item in fused}) == 20


def test_rrf_stable_ties():
    from catalyst_data.retrieval.fusion import fuse

    a = make_result("a:1", lexical_rank=1)
    b = make_result("b:1", lexical_rank=1)
    assert [item.chunk_id for item in fuse([b, a], [], k=60)] == ["a:1", "b:1"]


def test_rrf_shared_candidate_gets_both_contributions():
    from catalyst_data.retrieval.fusion import fuse

    shared = make_result("shared:1")
    fused = fuse([shared], [shared], k=60)
    assert fused[0].fusion_score == pytest.approx(2 / 61)


def test_rrf_third_arm_contribution_is_preserved():
    from catalyst_data.retrieval.fusion import fuse

    shared = make_result("shared:1")
    fused = fuse([shared], [], [shared], k=60, output_k=20)
    assert [item.chunk_id for item in fused] == ["shared:1"]
    assert fused[0].fusion_score == pytest.approx(2 / 61)
    assert dict(fused[0].arm_ranks) == {"lexical": 1, "arm_3": 1}
    assert dict(fused[0].arm_scores) == {"lexical": None, "arm_3": None}


def test_rrf_shared_chunk_merges_both_arm_scores_and_ranks():
    from catalyst_data.retrieval.fusion import fuse

    lexical = make_result("shared:score", lexical_rank=1, lexical_raw_score=-2.5)
    dense = make_result(
        "shared:score", mode_requested="dense", mode_served="dense",
        dense_rank=2, dense_score=0.91,
    )
    fused = fuse([lexical], [dense], k=60)
    assert fused[0].lexical_rank == 1
    assert fused[0].dense_rank == 2
    assert fused[0].lexical_raw_score == -2.5
    assert fused[0].dense_score == 0.91
    assert fused[0].mode_requested == "hybrid"
    assert fused[0].mode_served == "hybrid"
    assert fused[0].index_manifest_id == "1" * 64


def test_rrf_rejects_mixed_scope_or_index_identity():
    from catalyst_data.retrieval.fusion import fuse

    with pytest.raises(ValueError, match="identical corpus"):
        fuse([make_result("a", ticker="AAPL")], [make_result("a", ticker="MSFT")])
    with pytest.raises(ValueError, match="one index"):
        fuse(
            [make_result("a", index_manifest_id="1" * 64)],
            [make_result("a", mode_requested="dense", mode_served="dense", index_manifest_id="2" * 64)],
        )
    with pytest.raises(ValueError, match="one index"):
        fuse(
            [make_result("a", index_manifest_id=None)],
            [make_result("a", mode_requested="dense", mode_served="dense", index_manifest_id="1" * 64)],
        )
