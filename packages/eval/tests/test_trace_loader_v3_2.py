import importlib.util
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOADER_SCRIPT_PATH = PROJECT_ROOT / "scripts" / "load_refusal_cases.py"
TRACE_SCRIPT_PATH = PROJECT_ROOT / "scripts" / "p1_trace_report.py"


def load_script_module(script_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_loader_uses_validated_file_and_query_precedence():
    loader = load_script_module(str(LOADER_SCRIPT_PATH), "load_refusal_cases")
    trace = load_script_module(str(TRACE_SCRIPT_PATH), "p1_trace_report")

    rows = loader.load_refusal_cases(
        str(PROJECT_ROOT / "packages" / "eval" / "golden_set" / "h_refusal_cases.validated.json")
    )
    assert len(rows) == 8

    case = {"ticker": "NVDA", "trade_date": "2025-10-28", "query_override": "CASE_Q"}
    query, source = trace.resolve_query_with_source(case, "CLI_Q")
    assert query == "CLI_Q"
    assert source == "cli_override"

    query, source = trace.resolve_query_with_source(case, None)
    assert query == "CASE_Q"
    assert source == "case_override"

    case_no_override = {"ticker": "NVDA", "trade_date": "2025-10-29"}
    query, source = trace.resolve_query_with_source(case_no_override, None)
    assert query == "Why did NVDA move on 2025-10-29?"
    assert source == "default_template"

    state = trace._build_initial_state(
        case_no_override,
        query=query,
        db_path=trace.DEFAULT_DB_PATH,
        lancedb_dir=trace.DEFAULT_LANCEDB_DIR,
        model="gemini-2.5-flash-nothink",
        window_days=3,
    )
    assert state["query"] == "Why did NVDA move on 2025-10-29?"


def test_trace_summary_contract_includes_guardrail_fields():
    trace = load_script_module(str(TRACE_SCRIPT_PATH), "p1_trace_report_guardrails")
    state = trace._build_initial_state(
        {"ticker": "NVDA", "trade_date": "2025-10-29"},
        query="Why did NVDA move on 2025-10-29?",
        db_path=trace.DEFAULT_DB_PATH,
        lancedb_dir=trace.DEFAULT_LANCEDB_DIR,
        model="gemini-2.5-flash-nothink",
        window_days=3,
    )
    assert "query_ticker_raw" in state
    assert "ticker_consistent" in state
    assert "market_session_valid" in state
    assert "magnitude_plausible" in state
