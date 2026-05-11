#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "agents"))
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "eval"))

from catalyst_agents.cost_tracker import MODEL_PRICING
from catalyst_eval.harness.frozen_eval import utc_now_iso

DEFAULT_MODELS = [
    "gemini-2.5-flash-nothink",
    "deepseek-v4-flash",
    "qwen3.6-flash",
    "qwen-turbo",
    "DeepSeek-V3.1-Terminus",
    "claude-opus-4-6",
]
DEFAULT_PROVIDER = "aihubmix"
DEFAULT_BASE_URL = "https://aihubmix.com/v1"
DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "eval_reports"
DEFAULT_QUERY = "Why did NVDA move on 2025-10-28? Provide 3 grounded causes with evidence IDs."


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run direct LLM baseline for g013 multi-model comparison.")
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS), help="Comma-separated model ids")
    parser.add_argument("--provider", default=DEFAULT_PROVIDER)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--query", default=None)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--llm-seed", type=int, default=None)
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--tag", default=None)
    return parser.parse_args()


def _resolve_key(provider: str) -> str:
    if provider != "aihubmix":
        raise ValueError(f"unsupported provider: {provider}")
    key = os.environ.get("AIHUBMIX_API_KEY") or os.environ.get("aihubmix_api_key")
    if not key:
        raise RuntimeError("Missing AIHubMix key. Set AIHUBMIX_API_KEY.")
    return key


def _extract_text(resp: Any) -> str:
    try:
        content = resp.choices[0].message.content
        if isinstance(content, str):
            return content
    except Exception:
        pass
    return ""


def _usage_int(resp: Any, field: str) -> int:
    try:
        return int(getattr(resp.usage, field, 0) or 0)
    except Exception:
        return 0


def _resolve_pricing_model_id(model_id: str) -> str:
    if model_id in MODEL_PRICING:
        return model_id
    if model_id.startswith("gemini-2.5-flash"):
        return "gemini-2.5-flash"
    return model_id


def _compute_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    pricing = MODEL_PRICING.get(_resolve_pricing_model_id(model_id))
    if pricing is None:
        return 0.0
    in_cost = float(input_tokens) * float(pricing.input_per_1m) / 1_000_000.0
    out_cost = float(output_tokens) * float(pricing.output_per_1m) / 1_000_000.0
    return in_cost + out_cost


def _classify_status(answer_text: str) -> str:
    lowered = answer_text.lower()
    if any(token in lowered for token in ["insufficient", "not enough evidence", "cannot determine", "no evidence"]):
        return "INSUFFICIENT"
    return "SUFFICIENT"


def _has_hallucination(query: str, answer_text: str) -> bool:
    text = answer_text.lower()
    if re.search(r"\b\d{7}-\d{2}-\d{6}\b", answer_text):
        return True

    query_percents = set(re.findall(r"\b\d+(?:\.\d+)?%", query))
    for pct in re.findall(r"\b\d+(?:\.\d+)?%", answer_text):
        try:
            value = float(pct.rstrip("%"))
        except ValueError:
            continue
        if value >= 40.0 and pct not in query_percents:
            return True

    if "acquired openai" in text or "acquisition of openai" in text:
        return True
    return False


def _invoke_direct_model(
    *,
    model_id: str,
    query: str,
    provider: str,
    base_url: str,
    temperature: float,
    max_output_tokens: int,
    llm_seed: int | None,
) -> dict[str, Any]:
    from openai import OpenAI

    api_key = _resolve_key(provider)
    client = OpenAI(api_key=api_key, base_url=base_url)

    started = time.perf_counter()
    req: dict[str, Any] = {
        "model": model_id,
        "messages": [
            {
                "role": "user",
                "content": query,
            }
        ],
        "temperature": temperature,
        "max_tokens": max_output_tokens,
    }
    if llm_seed is not None:
        req["seed"] = llm_seed

    resp = client.chat.completions.create(**req)
    latency_ms = int((time.perf_counter() - started) * 1000)
    answer_text = _extract_text(resp)
    in_tokens = _usage_int(resp, "prompt_tokens")
    out_tokens = _usage_int(resp, "completion_tokens")
    total_tokens = _usage_int(resp, "total_tokens") or (in_tokens + out_tokens)
    total_cost_usd = _compute_cost(model_id, in_tokens, out_tokens)

    return {
        "model_id": model_id,
        "answer_text": answer_text,
        "latency_ms": latency_ms,
        "total_tokens": total_tokens,
        "total_cost_usd": total_cost_usd,
        "status_class": _classify_status(answer_text),
        "hallucination_flag": _has_hallucination(query, answer_text),
        "error_type": None,
        "error_message": None,
    }


def main() -> int:
    args = _parse_args()
    query = args.query or DEFAULT_QUERY
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = args.tag or time.strftime("%Y%m%d_%H%M%S", time.gmtime())

    model_ids = [m.strip() for m in args.models.split(",") if m.strip()]
    if not model_ids:
        raise ValueError("--models resolved to empty list")

    per_model: list[dict[str, Any]] = []
    failed_models: list[dict[str, Any]] = []

    for model_id in model_ids:
        try:
            row = _invoke_direct_model(
                model_id=model_id,
                query=query,
                provider=args.provider,
                base_url=args.base_url,
                temperature=args.temperature,
                max_output_tokens=args.max_output_tokens,
                llm_seed=args.llm_seed,
            )
        except Exception as exc:
            if not args.continue_on_error:
                raise
            row = {
                "model_id": model_id,
                "answer_text": "",
                "latency_ms": None,
                "total_tokens": None,
                "total_cost_usd": None,
                "status_class": "SYSTEM_ERROR",
                "hallucination_flag": False,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            }
            failed_models.append(
                {
                    "model_id": model_id,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }
            )
        per_model.append(row)

    payload = {
        "tag": tag,
        "generated_at_utc": utc_now_iso(),
        "provider": args.provider,
        "base_url": args.base_url,
        "query": query,
        "per_model": per_model,
        "failed_models": failed_models,
        "run_status": "completed_with_errors" if failed_models else "completed",
    }

    out_path = out_dir / f"{tag}_g013_direct_baseline.json"
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"[ok] direct_baseline_json={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
