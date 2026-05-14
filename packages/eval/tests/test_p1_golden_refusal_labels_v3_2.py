import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
GOLDEN = PROJECT_ROOT / "packages" / "eval" / "golden_set" / "v1_2_p1_set.jsonl"


def _load(case_id: str):
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row["id"] == case_id:
            return row
    raise AssertionError(case_id)


def test_g009_not_refusal_case():
    g009 = _load("g009")
    assert g009["should_refuse"] is False
    assert g009["expected_status"] == "SUFFICIENT"
