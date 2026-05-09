import importlib.util
import sys

LOADER_SCRIPT_PATH = "/Users/yiannischen/Desktop/Catalyst/scripts/load_refusal_cases.py"
TRACE_SCRIPT_PATH = "/Users/yiannischen/Desktop/Catalyst/scripts/p1_trace_report.py"


def load_script_module(script_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_loader_uses_validated_file_and_query_precedence():
    loader = load_script_module(LOADER_SCRIPT_PATH, "load_refusal_cases")
    trace = load_script_module(TRACE_SCRIPT_PATH, "p1_trace_report")

    rows = loader.load_refusal_cases(
        "/Users/yiannischen/Desktop/Catalyst/packages/eval/golden_set/h_refusal_cases.validated.json"
    )
    assert len(rows) == 8

    case = {"ticker": "NVDA", "trade_date": "2025-10-28", "query_override": "CASE_Q"}
    assert trace.resolve_query(case, "CLI_Q") == "CLI_Q"
    assert trace.resolve_query(case, None) == "CASE_Q"
