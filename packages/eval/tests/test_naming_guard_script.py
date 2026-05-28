from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_naming_guard_script_exists_and_checks_remediation_context():
    p = PROJECT_ROOT / "scripts/reports/check_naming_consistency.sh"
    text = p.read_text(encoding="utf-8")
    assert "remediation" in text.lower() or "整改" in text
    assert "P0" in text and "P1" in text and "P2" in text
