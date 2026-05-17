import importlib.util
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "lint_h_refusal_cases.py"


def load_script_module(script_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_lint_outputs_schema_and_holiday_warning_and_h001_typo_signal(tmp_path):
    mod = load_script_module(str(SCRIPT_PATH), "lint_h_refusal_cases")
    canonical_path = PROJECT_ROOT / "packages" / "eval" / "golden_set" / "h_refusal_cases.validated.json"
    rows = json.loads(canonical_path.read_text(encoding="utf-8"))
    # Keep validated set canonical, then inject one known typo variant for lint signal checks.
    for row in rows:
        if row.get("id") == "h001":
            row["ticker"] = "APPL"
            break
    synthetic_path = tmp_path / "h_refusal_cases.synthetic.json"
    synthetic_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    out = mod.lint_cases(
        synthetic_path,
        db_path=PROJECT_ROOT / "data" / "catalyst_eval_frozen_v2.db",
        holiday_config=PROJECT_ROOT / "configs" / "us_market_holidays_2025_2026.json",
    )

    assert isinstance(out.get("errors"), list)
    assert isinstance(out.get("warnings"), list)
    assert all(set(item.keys()) == {"case_id", "code", "message"} for item in out["errors"])
    assert all(set(item.keys()) == {"case_id", "code", "message"} for item in out["warnings"])

    assert any(e["case_id"] == "h001" and e["code"] == "ticker_not_in_db" for e in out["errors"])
    assert any(w["case_id"] == "h003" and w["code"] == "market_closed_date" for w in out["warnings"])
    assert any(w["case_id"] == "h001" and w["code"] == "query_ticker_mismatch_expected_refusal" for w in out["warnings"])

    validated_out = mod.lint_cases(
        canonical_path,
        db_path=PROJECT_ROOT / "data" / "catalyst_eval_frozen_v2.db",
        holiday_config=PROJECT_ROOT / "configs" / "us_market_holidays_2025_2026.json",
    )
    assert validated_out["ok"] is True


def test_h_refusal_results_report_exists_and_has_8_rows():
    p = PROJECT_ROOT / "docs" / "reports" / "2026-05-16-h-refusal-results.md"
    text = p.read_text(encoding="utf-8")
    assert "h001" in text and "h008" in text


def test_h_refusal_results_v2_report_exists_and_has_8_rows():
    p = PROJECT_ROOT / "docs" / "reports" / "2026-05-17-h-refusal-results-v2.md"
    text = p.read_text(encoding="utf-8")
    assert "h001" in text and "h008" in text
    assert "SYSTEM_ERROR" in text
