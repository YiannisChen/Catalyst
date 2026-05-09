import importlib.util

SCRIPT_PATH = "/Users/yiannischen/Desktop/Catalyst/packages/eval/catalyst_eval/langsmith_bridge.py"


def load_script_module(script_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_langsmith_disabled_returns_null_project():
    mod = load_script_module(SCRIPT_PATH, "langsmith_bridge")
    out = mod.resolve_langsmith_header(env={"LANGCHAIN_TRACING_V2": "false"})
    assert out == {"langsmith_enabled": False, "langsmith_project": None}
