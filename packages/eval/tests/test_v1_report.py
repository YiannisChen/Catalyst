"""M7-8: Stage-1 report write-once publication.

Report JSON uses canonical bytes: an absent target is created atomically,
identical bytes are idempotent, differing bytes raise a conflict without
overwrite. Markdown is a deterministic rendering of JSON and cannot supply
new facts.
"""
from __future__ import annotations

import json

import pytest

from catalyst_eval.v1_1.report import (
    ReportConflictError,
    render_report_markdown,
    scan_report_for_secrets,
    write_report_json,
    write_report_markdown,
)


def _payload() -> dict:
    return {
        "schema_version": "v1_1_stage1_report_v1",
        "eval_id": "eval:stage1:v1",
        "hard_gates": {"citation_correctness": True},
        "coverage_limited_count": 1,
        "model_limited_count": 11,
    }


def test_write_report_json_creates_atomically(tmp_path):
    path = tmp_path / "report.json"
    write_report_json(path, _payload())
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["eval_id"] == "eval:stage1:v1"


def test_write_report_json_is_idempotent_for_identical_bytes(tmp_path):
    path = tmp_path / "report.json"
    write_report_json(path, _payload())
    first = path.read_bytes()
    write_report_json(path, _payload())
    assert path.read_bytes() == first


def test_write_report_json_conflicts_without_overwrite(tmp_path):
    path = tmp_path / "report.json"
    write_report_json(path, _payload())
    other = dict(_payload())
    other["eval_id"] = "eval:stage1:v2"
    with pytest.raises(ReportConflictError):
        write_report_json(path, other)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["eval_id"] == "eval:stage1:v1"


def test_write_report_json_loses_race_without_overwriting_winner(
    tmp_path, monkeypatch
):
    """A competing publisher that wins after the pre-check is immutable."""
    from pathlib import Path
    from catalyst_eval.v1_1 import report as report_module

    path = tmp_path / "report.json"
    winner = b'{"winner":true}'

    def competing_link(_source, target):
        Path(target).write_bytes(winner)
        raise FileExistsError

    monkeypatch.setattr(report_module.os, "link", competing_link)
    with pytest.raises(ReportConflictError):
        write_report_json(path, _payload())
    assert path.read_bytes() == winner


def test_markdown_is_deterministic_rendering_of_json(tmp_path):
    payload = _payload()
    md_a = render_report_markdown(payload)
    md_b = render_report_markdown(payload)
    assert md_a == md_b
    # Markdown cannot introduce facts absent from the JSON payload.
    assert "not-a-real-fact" not in md_a
    assert "eval:stage1:v1" in md_a


def test_write_report_markdown_writes_plain_markdown_idempotently(tmp_path):
    path = tmp_path / "report.md"
    markdown = render_report_markdown(_payload())
    write_report_markdown(path, markdown)
    assert path.read_text(encoding="utf-8") == markdown
    assert path.read_bytes().startswith(b"# v1_1_stage1_report_v1")
    write_report_markdown(path, markdown)
    assert path.read_text(encoding="utf-8") == markdown


def test_secret_scan_flags_planted_secret():
    payload = {"text": "Bearer sk-planted-abcdef123456"}
    assert scan_report_for_secrets(payload, secret_values=("sk-planted-abcdef123456",))


def test_secret_scan_accepts_clean_report():
    payload = _payload()
    assert scan_report_for_secrets(payload, secret_values=("sk-real",)) == []
