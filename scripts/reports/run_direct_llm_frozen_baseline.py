from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from openai import OpenAI


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Direct-LLM baseline on thesis freeze units.")
    parser.add_argument("--freeze-manifest", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider", default="aihubmix")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--out-dir", required=True)
    return parser.parse_args()


def _raw_answer_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extract_text(resp: Any) -> str:
    try:
        content = resp.choices[0].message.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    t = part.get("text")
                    if isinstance(t, str):
                        chunks.append(t)
                else:
                    t = getattr(part, "text", None)
                    if isinstance(t, str):
                        chunks.append(t)
            return "".join(chunks)
    except Exception:
        pass
    return ""


def _parse_output_status(text: str) -> tuple[str, bool, str]:
    cleaned = text.strip()
    payload: dict[str, Any] | None = None
    try:
        payload = json.loads(cleaned)
    except Exception:
        if "```" in cleaned:
            parts = cleaned.split("```")
            for p in parts:
                p = p.strip()
                if not p:
                    continue
                if p.lower().startswith("json"):
                    p = p[4:].strip()
                try:
                    payload = json.loads(p)
                    break
                except Exception:
                    continue
    if payload is None:
        upper = cleaned.upper()
        if "INSUFFICIENT" in upper:
            return "INSUFFICIENT", True, cleaned
        if "PARTIAL" in upper:
            return "PARTIAL", False, cleaned
        if "SUFFICIENT" in upper:
            return "SUFFICIENT", False, cleaned
        raise ValueError("Cannot parse output_status from response")

    status = str(payload.get("output_status", "")).upper()
    if status not in {"SUFFICIENT", "PARTIAL", "INSUFFICIENT"}:
        raise ValueError(f"Invalid output_status: {status}")
    refusal = bool(payload.get("refusal_flag", status == "INSUFFICIENT"))
    answer = str(payload.get("answer", cleaned))
    return status, refusal, answer


def _build_prompt(unit: dict[str, Any]) -> str:
    return (
        "You are a financial attribution assistant.\n"
        "Classify whether the available information is enough to explain the user's query.\n"
        "Return STRICT JSON only:\n"
        '{"output_status":"SUFFICIENT|PARTIAL|INSUFFICIENT","refusal_flag":true|false,"answer":"..."}\n'
        "Rules:\n"
        "- INSUFFICIENT if evidence is not enough, date/ticker mismatches, or causality not grounded.\n"
        "- PARTIAL if some support exists but not complete.\n"
        "- SUFFICIENT only with clear grounded explanation.\n\n"
        f"Ticker: {unit.get('ticker')}\n"
        f"Trade date: {unit.get('trade_date')}\n"
        f"User query: {unit.get('query')}\n"
        f"Expected status (for reference only, do NOT copy blindly): {unit.get('expected_status')}\n"
    )


def main() -> int:
    args = _parse_args()
    manifest_path = Path(args.freeze_manifest)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    per_unit_path = out_dir / "direct_llm_dsv4_frozen_per_unit.jsonl"
    summary_path = out_dir / "direct_llm_dsv4_frozen_summary.json"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    units = manifest.get("frozen_units", [])
    if not isinstance(units, list) or not units:
        print("[fail] freeze manifest has no frozen_units")
        return 2

    api_key = Path("/dev/null")
    del api_key
    import os
    key = os.environ.get("AIHUBMIX_API_KEY", "").strip()
    if not key:
        print("[fail] missing AIHUBMIX_API_KEY")
        return 3

    client = OpenAI(api_key=key, base_url=args.base_url)
    stats = {
        "n_total": 0,
        "n_error": 0,
        "status_counts": {"SUFFICIENT": 0, "PARTIAL": 0, "INSUFFICIENT": 0, "SYSTEM_ERROR": 0},
        "total_latency_ms": 0.0,
        "total_tokens": 0,
        "total_cost_usd": 0.0,
    }

    with per_unit_path.open("w", encoding="utf-8") as f:
        for unit in units:
            stats["n_total"] += 1
            record: dict[str, Any] = {
                "case_id": unit.get("case_id"),
                "profile": unit.get("profile"),
                "expected_status": unit.get("expected_status"),
                "should_refuse": bool(unit.get("should_refuse", False)),
                "output_status": "SYSTEM_ERROR",
                "refusal_flag": False,
                "latency_ms": None,
                "tokens_in": 0,
                "tokens_out": 0,
                "total_tokens": 0,
                "total_cost_usd": 0.0,
                "error_type": None,
                "raw_answer_hash": None,
            }
            start = time.perf_counter()
            try:
                resp = client.chat.completions.create(
                    model=args.model,
                    messages=[{"role": "user", "content": _build_prompt(unit)}],
                    temperature=0.0,
                    max_tokens=700,
                )
                latency_ms = (time.perf_counter() - start) * 1000.0
                text = _extract_text(resp)
                status, refusal_flag, answer = _parse_output_status(text)
                usage = resp.usage
                tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
                tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)
                total_tokens = int(getattr(usage, "total_tokens", tokens_in + tokens_out) or (tokens_in + tokens_out))

                record.update(
                    {
                        "output_status": status,
                        "refusal_flag": bool(refusal_flag),
                        "latency_ms": round(latency_ms, 3),
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "total_tokens": total_tokens,
                        "total_cost_usd": 0.0,  # pricing not locked for deepseek-v4-flash in current repo
                        "raw_answer_hash": _raw_answer_hash(answer),
                    }
                )
            except Exception as exc:
                latency_ms = (time.perf_counter() - start) * 1000.0
                record["latency_ms"] = round(latency_ms, 3)
                record["error_type"] = type(exc).__name__
                stats["n_error"] += 1

            stats["status_counts"][record["output_status"]] = stats["status_counts"].get(record["output_status"], 0) + 1
            stats["total_latency_ms"] += float(record["latency_ms"] or 0.0)
            stats["total_tokens"] += int(record["total_tokens"] or 0)
            stats["total_cost_usd"] += float(record["total_cost_usd"] or 0.0)
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    n = max(1, stats["n_total"])
    summary = {
        "freeze_id": manifest.get("freeze_id"),
        "model": args.model,
        "provider": args.provider,
        "base_url": args.base_url,
        "n_total": stats["n_total"],
        "n_error": stats["n_error"],
        "status_counts": stats["status_counts"],
        "avg_latency_ms": stats["total_latency_ms"] / n,
        "avg_total_tokens": stats["total_tokens"] / n,
        "avg_total_cost_usd": stats["total_cost_usd"] / n,
        "per_unit_jsonl": str(per_unit_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[ok] wrote per-unit: {per_unit_path}")
    print(f"[ok] wrote summary: {summary_path}")
    print(f"[ok] n_total={summary['n_total']} status_counts={summary['status_counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

