from __future__ import annotations

import importlib.util
from pathlib import Path


class _Usage:
    prompt_tokens = 20
    completion_tokens = 10
    total_tokens = 30


class _Message:
    content = '{"output_status":"INSUFFICIENT","refusal_flag":true,"answer":"no evidence"}'


class _Choice:
    message = _Message()


class _Resp:
    usage = _Usage()
    choices = [_Choice()]
    id = "req_test"


class _Completions:
    def create(self, **kwargs):
        return _Resp()


class _Chat:
    completions = _Completions()


class _Client:
    chat = _Chat()


def _load_module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "reports" / "run_direct_llm_frozen_baseline.py"
    spec = importlib.util.spec_from_file_location("run_direct_llm_frozen_baseline", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_cli_accepts_baseline_mode_and_search_fields():
    mod = _load_module()
    args = mod._parse_args([
        "--freeze-manifest", "m.json",
        "--model", "gemini-2.5-flash",
        "--catalyst-model", "gemini-2.5-flash",
        "--base-url", "https://example.com/v1",
        "--out-dir", "out",
        "--baseline-mode", "closed_book",
    ])
    assert args.baseline_mode == "closed_book"


def test_tier0_model_must_match_catalyst():
    mod = _load_module()
    row = mod._run_one_unit(
        unit={"case_id": "g001", "profile": "full", "expected_status": "INSUFFICIENT", "ticker": "TSLA", "trade_date": "2025-01-02", "query": "why"},
        model="gemini-2.5-flash",
        catalyst_model="gemini-2.5-flash",
        baseline_mode="closed_book",
        client=_Client(),
        pricing={"gemini-2.5-flash": {"input": 0.1, "output": 0.2}},
    )
    assert row["model"] == "gemini-2.5-flash"


def test_per_unit_row_includes_baseline_and_evidence_meta():
    mod = _load_module()
    row = mod._run_one_unit(
        unit={"case_id": "g001", "profile": "full", "expected_status": "INSUFFICIENT", "ticker": "TSLA", "trade_date": "2025-01-02", "query": "why"},
        model="gemini-2.5-flash",
        catalyst_model="gemini-2.5-flash",
        baseline_mode="closed_book",
        client=_Client(),
        pricing={"gemini-2.5-flash": {"input": 0.1, "output": 0.2}},
    )
    assert row["baseline_mode"] in {"closed_book", "search_augmented", "same_evidence"}
    assert "model" in row
    assert "search_hits_count" in row
    assert "evidence_chunks_count" in row
