import importlib.util
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "verify_model_lock.py"


def load_script_module(script_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_model_verifier_contract_and_error_branches(tmp_path):
    mod = load_script_module(str(SCRIPT_PATH), "verify_model_lock")

    lock_path = tmp_path / "model_profiles.lock.json"
    lock_path.write_text(
        """{
  \"profiles\": [
    {\"model_id\": \"m1\"},
    {\"model_id\": \"m2\"},
    {\"model_id\": \"m3\"},
    {\"model_id\": \"m4\"}
  ]
}""",
        encoding="utf-8",
    )

    with patch.object(mod, "safe_get_models", return_value={"status": "ok", "http_status": 200, "models": ["m1", "m2", "m3", "m4"]}), \
         patch.object(mod, "safe_smoke_call", side_effect=[
             {"status": "network_error", "in_models_endpoint": True, "http_status": None, "error_code": "network_error"},
             {"status": "http_404", "in_models_endpoint": False, "http_status": 404, "error_code": "model_not_found"},
             {"status": "http_429", "in_models_endpoint": True, "http_status": 429, "error_code": "rate_limit"},
             {"status": "http_5xx", "in_models_endpoint": True, "http_status": 503, "error_code": "upstream_unavailable"},
         ]):
        out = mod.verify_model_availability(
            lock_path=str(lock_path),
            base_url="https://api.mock",
            api_key="k",
            timeout_sec=5,
        )

    assert set(out.keys()) == {"ok", "models_endpoint", "profiles", "errors"}
    assert all(set(row.keys()) == {"model_id", "in_models_endpoint", "smoke_status", "http_status", "error_code", "verified_at"} for row in out["profiles"])
    assert {e["code"] for e in out["errors"]}.issuperset({"network_error", "http_404", "http_429", "http_5xx"})
