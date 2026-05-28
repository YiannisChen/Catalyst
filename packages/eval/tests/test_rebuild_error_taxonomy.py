import csv
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _rows():
    p = PROJECT_ROOT / "docs/reports/2026-05-15-catalyst-error-taxonomy.csv"
    return list(csv.DictReader(p.open()))


def test_router_path_bucket_is_empty():
    rows = _rows()
    assert sum(1 for r in rows if r["first_failure_point"] == "ROUTER_PATH") == 0


def test_g050_degraded_is_miner_coverage():
    rows = _rows()
    hit = [r for r in rows if r["profile"] == "degraded" and r["case_id"] == "g050"]
    assert len(hit) == 1
    assert hit[0]["first_failure_point"] == "MINER_COVERAGE"
