from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_master_doc_contains_r0_r1_r2_and_deliverables():
    p = PROJECT_ROOT / "docs/plans/2026-05-15-r0-r1-r2-remediation-master.md"
    text = p.read_text(encoding="utf-8")
    assert "R0" in text
    assert "R1" in text
    assert "R2" in text
    assert "Deliverables" in text
