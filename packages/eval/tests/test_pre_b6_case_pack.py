"""40 probes + 12 attribution case pack schema."""

from __future__ import annotations

import json
from pathlib import Path

from catalyst_data.manifests.universe import RATIFIED_TICKERS

GOLDEN = Path(__file__).resolve().parents[1] / "golden_set"


def test_exactly_40_probes_match_universe():
    path = GOLDEN / "pre_b6_coverage_probes_v1.json"
    data = json.loads(path.read_text())
    tickers = data["tickers"] if isinstance(data, dict) else data
    assert len(tickers) == 40
    assert set(tickers) == set(RATIFIED_TICKERS)


def test_twelve_cases_mutex_histogram_4_2_2_2_2():
    path = GOLDEN / "pre_b6_attribution_cases_v1.jsonl"
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    assert len(rows) == 12
    from collections import Counter

    c = Counter(r["slot"] for r in rows)
    assert c["single_source_answerable"] == 4
    assert c["multi_source_answerable"] == 2
    assert c["correct_abstain"] == 2
    assert c["unsupported_distractor"] == 2
    assert c["temporal_lookahead_trap"] == 2


def test_no_documented_empty_pass_flag_in_probe_schema():
    path = GOLDEN / "pre_b6_coverage_probes_v1.json"
    text = path.read_text()
    assert "documented_empty_pass" not in text
    assert "allow_empty" not in text
