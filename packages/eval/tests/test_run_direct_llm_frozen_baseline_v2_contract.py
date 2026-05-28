from __future__ import annotations

import importlib.util
from pathlib import Path


class _Usage:
    prompt_tokens = 100
    completion_tokens = 50
    total_tokens = 150


class _Message:
    content = '{"output_status":"INSUFFICIENT","refusal_flag":true,"answer":"not enough"}'


class _Choice:
    message = _Message()


class _Resp:
    usage = _Usage()
    choices = [_Choice()]
    id = "req_test_123"


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


def test_prompt_has_no_expected_status_leak():
    mod = _load_module()
    prompt = mod._build_prompt({"ticker": "AAPL", "trade_date": "2026-01-15", "query": "why"})
    assert "Expected status" not in prompt


def test_per_unit_record_contains_audit_fields():
    mod = _load_module()
    row = mod._run_one_unit(
        unit={"case_id": "g001", "profile": "full", "expected_status": "SUFFICIENT", "should_refuse": False, "ticker": "AAPL", "trade_date": "2026-01-15", "query": "why"},
        model="deepseek-v4-flash",
        client=_Client(),
        pricing={"deepseek-v4-flash": {"input": 0.27, "output": 1.1}},
    )
    assert "raw_answer_text" in row
    assert "parse_strategy" in row
    assert "status_decision_trace" in row
    assert "request_id" in row
