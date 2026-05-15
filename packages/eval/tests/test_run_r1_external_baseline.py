from pathlib import Path
import importlib.util
import json
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = PROJECT_ROOT / "scripts/reports/run_r1_external_baseline.py"
SPEC = importlib.util.spec_from_file_location("run_r1_external_baseline", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MODULE)
main = MODULE.main


def _write_input(path: Path) -> None:
    row = {
        "case_id": "g001",
        "chunks": [
            {"chunk_id": "asset-1::l2s0001", "chunk_text": "A", "relevance": 0.9},
            {"chunk_id": "asset-2::l2s0002", "chunk_text": "B", "relevance": 0.1},
        ],
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")


def test_mock_output_is_deterministic_and_limit_applies(tmp_path):
    input_path = tmp_path / "in.jsonl"
    out1 = tmp_path / "out1.jsonl"
    out2 = tmp_path / "out2.jsonl"
    _write_input(input_path)

    rc1 = main([
        "--input",
        str(input_path),
        "--output",
        str(out1),
        "--model",
        "mock-sonnet",
        "--mock",
        "--limit",
        "1",
    ])
    rc2 = main([
        "--input",
        str(input_path),
        "--output",
        str(out2),
        "--model",
        "mock-sonnet",
        "--mock",
        "--limit",
        "1",
    ])

    assert rc1 == 0
    assert rc2 == 0

    rows1 = [json.loads(x) for x in out1.read_text(encoding="utf-8").splitlines() if x.strip()]
    rows2 = [json.loads(x) for x in out2.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(rows1) == 1
    assert rows1 == rows2
    assert rows1[0]["case_id"] == "g001"
    assert rows1[0]["chunk_id"] == "asset-1::l2s0001"
    assert 0.0 <= float(rows1[0]["relevance"]) <= 1.0


def test_non_mock_mode_fails_with_cloud_only_message(tmp_path, capsys):
    input_path = tmp_path / "in.jsonl"
    out = tmp_path / "out.jsonl"
    _write_input(input_path)

    rc = main([
        "--input",
        str(input_path),
        "--output",
        str(out),
        "--model",
        "real-sonnet",
    ])
    captured = capsys.readouterr()

    assert rc != 0
    message = (captured.out + captured.err).lower()
    assert "choose one mode" in message


def test_real_mode_requires_cloud_flag_and_key(tmp_path):
    input_path = tmp_path / "in.jsonl"
    out = tmp_path / "out.jsonl"
    _write_input(input_path)

    rc = main([
        "--input",
        str(input_path),
        "--output",
        str(out),
        "--model",
        "claude-sonnet-4-20250514",
    ])
    assert rc != 0


def test_real_mode_with_stubbed_api_writes_expected_schema(tmp_path, monkeypatch):
    input_path = tmp_path / "in.jsonl"
    out = tmp_path / "out.jsonl"
    _write_input(input_path)

    monkeypatch.setenv("AIHUBMIX_API_KEY", "fake")

    def fake_grade_chunk_real(**kwargs):
        return 0.73

    monkeypatch.setattr(MODULE, "_grade_chunk_real", fake_grade_chunk_real, raising=False)

    rc = main([
        "--input",
        str(input_path),
        "--output",
        str(out),
        "--model",
        "claude-sonnet-4-20250514",
        "--real",
        "--limit",
        "1",
    ])
    assert rc == 0

    row = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert set(row.keys()) >= {"case_id", "chunk_id", "relevance", "model", "mode"}
    assert row["mode"] == "real"


def test_parse_relevance_accepts_valid_json_score():
    score = MODULE._parse_relevance_from_response('{"relevance": 0.73}')
    assert score == 0.73


def test_parse_relevance_rejects_invalid_payload():
    with pytest.raises(ValueError):
        MODULE._parse_relevance_from_response("not-json")


def test_parse_relevance_accepts_json_in_markdown_fence():
    payload = """```json
{"relevance": 0.61}
```"""
    score = MODULE._parse_relevance_from_response(payload)
    assert score == 0.61


def test_parse_relevance_accepts_json_embedded_in_text():
    payload = 'Here is score: {"relevance": 0.44} thanks.'
    score = MODULE._parse_relevance_from_response(payload)
    assert score == 0.44


def test_parse_relevance_accepts_freeform_numeric_fallback():
    payload = "I estimate relevance around 0.27 given weak causal support."
    score = MODULE._parse_relevance_from_response(payload)
    assert score == 0.27
