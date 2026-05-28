from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCK_PATH = PROJECT_ROOT / "configs" / "model_profiles.lock.json"


def now_iso_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_get_models(base_url: str, api_key: str, timeout_sec: int) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        r = requests.get(url, headers=headers, timeout=timeout_sec)
    except requests.RequestException as exc:
        return {"status": "network_error", "http_status": None, "error": str(exc), "models": []}

    if r.status_code >= 500:
        return {"status": "http_5xx", "http_status": r.status_code, "error": r.text[:300], "models": []}
    if r.status_code == 404:
        return {"status": "http_404", "http_status": 404, "error": r.text[:300], "models": []}
    if r.status_code == 429:
        return {"status": "http_429", "http_status": 429, "error": r.text[:300], "models": []}
    if r.status_code >= 400:
        return {"status": f"http_{r.status_code}", "http_status": r.status_code, "error": r.text[:300], "models": []}

    payload = r.json() if r.text.strip() else {}
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    models = [row.get("id") for row in rows if isinstance(row, dict) and row.get("id")]
    return {"status": "ok", "http_status": r.status_code, "error": None, "models": models}


def safe_smoke_call(base_url: str, api_key: str, model_id: str, timeout_sec: int) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=timeout_sec)
    except requests.RequestException as exc:
        return {
            "status": "network_error",
            "in_models_endpoint": False,
            "http_status": None,
            "error_code": "network_error",
            "error": str(exc),
        }

    if r.status_code == 404:
        return {
            "status": "http_404",
            "in_models_endpoint": False,
            "http_status": 404,
            "error_code": "model_not_found",
            "error": r.text[:300],
        }
    if r.status_code == 429:
        return {
            "status": "http_429",
            "in_models_endpoint": True,
            "http_status": 429,
            "error_code": "rate_limit",
            "error": r.text[:300],
        }
    if r.status_code >= 500:
        return {
            "status": "http_5xx",
            "in_models_endpoint": True,
            "http_status": r.status_code,
            "error_code": "upstream_unavailable",
            "error": r.text[:300],
        }
    if r.status_code >= 400:
        return {
            "status": f"http_{r.status_code}",
            "in_models_endpoint": True,
            "http_status": r.status_code,
            "error_code": "http_error",
            "error": r.text[:300],
        }

    return {
        "status": "ok",
        "in_models_endpoint": True,
        "http_status": r.status_code,
        "error_code": None,
        "error": None,
    }


def verify_model_availability(lock_path: str, base_url: str, api_key: str, timeout_sec: int) -> dict[str, Any]:
    lock = json.loads(Path(lock_path).read_text(encoding="utf-8"))
    profiles = lock.get("profiles", [])

    models_endpoint = safe_get_models(base_url, api_key, timeout_sec)
    available = set(models_endpoint.get("models", []))

    rows = []
    errors = []
    ok = models_endpoint.get("status") == "ok"

    for profile in profiles:
        model_id = profile.get("model_id")
        smoke = safe_smoke_call(base_url, api_key, model_id, timeout_sec)
        row = {
            "model_id": model_id,
            "in_models_endpoint": model_id in available if available else smoke.get("in_models_endpoint", False),
            "smoke_status": smoke.get("status"),
            "http_status": smoke.get("http_status"),
            "error_code": smoke.get("error_code"),
            "verified_at": now_iso_utc(),
        }
        rows.append(row)

        if row["smoke_status"] in {"network_error", "http_404", "http_429", "http_5xx"}:
            ok = False
            errors.append({"model_id": model_id, "code": row["smoke_status"]})

    if models_endpoint.get("status") in {"network_error", "http_404", "http_429", "http_5xx"}:
        ok = False
        errors.append({"model_id": "<models-endpoint>", "code": models_endpoint.get("status")})

    return {"ok": ok, "models_endpoint": models_endpoint, "profiles": rows, "errors": errors}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--lock-path", default=str(DEFAULT_LOCK_PATH))
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--timeout-sec", type=int, default=10)
    args = parser.parse_args()

    out = verify_model_availability(args.lock_path, args.base_url, args.api_key, args.timeout_sec)
    print(json.dumps(out, indent=2, ensure_ascii=False))
