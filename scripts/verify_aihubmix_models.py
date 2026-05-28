#!/usr/bin/env python3
"""Verify which aihubmix models are callable.

Sends a minimal prompt to each supported model and reports
success/failure with latency. Reads AIHUBMIX_API_KEY from env.

Usage:
    export AIHUBMIX_API_KEY="<your-key>"
    python scripts/verify_aihubmix_models.py
"""
from __future__ import annotations

import os
import sys
import time

from openai import OpenAI


AIHUBMIX_BASE_URL = "https://aihubmix.com/v1"

MODELS = [
    "gemini-2.5-flash-nothink",
    "claude-opus-4-6",
    "deepseek-v4-flash",
    "qwen3.6-flash",
    "qwen-turbo",
    "deepseek-v3",
    "coding-minimax-m2.7-free",
    "qwen3.6-plus-preview-free",
]

TEST_PROMPT = "Reply with exactly one word: hello"


def verify_model(client: OpenAI, model_id: str) -> dict:
    start = time.perf_counter()
    try:
        response = client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": TEST_PROMPT}],
            max_tokens=10,
            temperature=0.0,
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        content = response.choices[0].message.content.strip() if response.choices else ""
        return {
            "model": model_id,
            "status": "OK",
            "latency_ms": elapsed_ms,
            "response": content[:50],
        }
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return {
            "model": model_id,
            "status": "FAIL",
            "latency_ms": elapsed_ms,
            "error": str(exc)[:120],
        }


def main():
    api_key = os.environ.get("AIHUBMIX_API_KEY", "").strip()
    if not api_key:
        print("ERROR: AIHUBMIX_API_KEY environment variable is not set.")
        sys.exit(1)

    client = OpenAI(api_key=api_key, base_url=AIHUBMIX_BASE_URL)

    print(f"Testing {len(MODELS)} models against aihubmix...\n")
    print(f"{'Model':<35} {'Status':<8} {'Latency':<10} {'Response / Error'}")
    print("-" * 90)

    results = []
    for model_id in MODELS:
        result = verify_model(client, model_id)
        results.append(result)

        status_str = result["status"]
        latency_str = f"{result['latency_ms']}ms"
        detail = result.get("response", result.get("error", ""))
        print(f"{model_id:<35} {status_str:<8} {latency_str:<10} {detail}")

    ok_count = sum(1 for r in results if r["status"] == "OK")
    fail_count = len(results) - ok_count
    print(f"\nSummary: {ok_count} OK, {fail_count} FAIL out of {len(results)} models")

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
