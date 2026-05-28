from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_report_contains_system_vs_remediation_naming_contract():
    p = PROJECT_ROOT / "docs/reports/2026-05-15-catalyst-full-debug-analysis.md"
    text = p.read_text(encoding="utf-8")
    assert "System phase: P0/P1/P2" in text
    assert "Remediation phase: R0/R1/R2" in text
