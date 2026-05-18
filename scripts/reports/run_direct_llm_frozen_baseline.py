from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from openai import OpenAI


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Direct-LLM baseline on thesis freeze units.")
    parser.add_argument("--freeze-manifest", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--catalyst-model", required=True)
    parser.add_argument("--baseline-mode", choices=["closed_book", "search_augmented", "same_evidence"], default="closed_book")
    parser.add_argument("--search-corpus-jsonl")
    parser.add_argument("--same-evidence-jsonl")
    parser.add_argument("--provider", default="aihubmix")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--pricing-lock", default=str(Path(__file__).resolve().parents[2] / "configs" / "eval_pricing.lock.json"))
    return parser.parse_args(argv)


def _load_pricing_lock(path: Path) -> dict[str, dict[str, float]]:
    return json.loads(path.read_text(encoding="utf-8"))


def _compute_cost(model: str, in_tok: int, out_tok: int, pricing: dict[str, dict[str, float]]) -> float:
    model_pricing = pricing.get(model, {"input": 0.0, "output": 0.0})
    in_rate = float(model_pricing.get("input", 0.0))
    out_rate = float(model_pricing.get("output", 0.0))
    return ((in_tok / 1_000_000.0) * in_rate) + ((out_tok / 1_000_000.0) * out_rate)


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
    )


def _search_local_corpus(unit: dict[str, Any], corpus_rows: list[dict[str, Any]], top_k: int = 5) -> list[dict[str, Any]]:
    case_id = unit.get("case_id")
    profile = unit.get("profile")
    for row in corpus_rows:
        if row.get("case_id") == case_id and row.get("profile") == profile:
            cands = list(row.get("search_candidates") or [])
            cands.sort(key=lambda x: (int(x.get("source_rank", 9999)), str(x.get("chunk_id", ""))))
            return cands[:top_k]
    return []


def _build_prompt_with_context(
    unit: dict[str, Any],
    *,
    baseline_mode: str,
    search_rows: list[dict[str, Any]] | None = None,
    same_evidence_rows: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, int]]:
    prompt = _build_prompt(unit)
    meta = {"search_hits_count": 0, "evidence_chunks_count": 0}
    if baseline_mode == "search_augmented":
        hits = _search_local_corpus(unit, search_rows or [])
        meta["search_hits_count"] = len(hits)
        if hits:
            prompt += "\nSearch Evidence:\n"
            for h in hits:
                prompt += f"- [{h.get('chunk_id')}] {h.get('content', '')}\n"
    if baseline_mode == "same_evidence":
        rows = same_evidence_rows or []
        case_id = unit.get("case_id")
        profile = unit.get("profile")
        row = next((r for r in rows if r.get("case_id") == case_id and r.get("profile") == profile), None)
        chunks = list((row or {}).get("evidence_chunks") or [])
        meta["evidence_chunks_count"] = len(chunks)
        if chunks:
            prompt += "\nEvidence Chunks:\n"
            for c in chunks:
                prompt += f"- [{c.get('chunk_id')}] {c.get('content_md', '')}\n"
    return prompt, meta


def _run_one_unit(
    *,
    unit: dict[str, Any],
    model: str,
    catalyst_model: str,
    baseline_mode: str,
    client: Any,
    pricing: dict[str, dict[str, float]],
    search_rows: list[dict[str, Any]] | None = None,
    same_evidence_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
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
        "baseline_mode": baseline_mode,
        "model": model,
        "search_hits_count": 0,
        "evidence_chunks_count": 0,
        "raw_answer_hash": None,
        "raw_answer_text": "",
        "parse_strategy": "failed",
        "status_decision_trace": "",
        "request_id": None,
        "pricing_version": "eval_pricing.lock.v1",
    }
    start = time.perf_counter()
    try:
        prompt, prompt_meta = _build_prompt_with_context(
            unit,
            baseline_mode=baseline_mode,
            search_rows=search_rows,
            same_evidence_rows=same_evidence_rows,
        )
        record["search_hits_count"] = int(prompt_meta.get("search_hits_count", 0))
        record["evidence_chunks_count"] = int(prompt_meta.get("evidence_chunks_count", 0))
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
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
        parse_strategy = "json" if text.strip().startswith("{") else "heuristic"
        decision_trace = f"status={status};refusal={bool(refusal_flag)}"
        record.update(
            {
                "output_status": status,
                "refusal_flag": bool(refusal_flag),
                "latency_ms": round(latency_ms, 3),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "total_tokens": total_tokens,
                "total_cost_usd": _compute_cost(model, tokens_in, tokens_out, pricing),
                "raw_answer_hash": _raw_answer_hash(answer),
                "raw_answer_text": text,
                "parse_strategy": parse_strategy,
                "status_decision_trace": decision_trace,
                "request_id": getattr(resp, "id", None),
            }
        )
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000.0
        record["latency_ms"] = round(latency_ms, 3)
        record["error_type"] = type(exc).__name__
    return record


def main() -> int:
    args = _parse_args()
    if args.baseline_mode == "closed_book" and args.model != args.catalyst_model:
        raise ValueError("Tier0 fairness violation: direct model must equal catalyst model")
    if args.baseline_mode == "search_augmented" and not args.search_corpus_jsonl:
        raise ValueError("search_augmented mode requires --search-corpus-jsonl")
    if args.baseline_mode == "same_evidence" and not args.same_evidence_jsonl:
        raise ValueError("same_evidence mode requires --same-evidence-jsonl")
    manifest_path = Path(args.freeze_manifest)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    per_unit_path = out_dir / "direct_llm_dsv4_frozen_per_unit.jsonl"
    summary_path = out_dir / "direct_llm_dsv4_frozen_summary.json"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pricing = _load_pricing_lock(Path(args.pricing_lock))
    units = manifest.get("frozen_units", [])
    search_rows: list[dict[str, Any]] = []
    same_evidence_rows: list[dict[str, Any]] = []
    if not isinstance(units, list) or not units:
        print("[fail] freeze manifest has no frozen_units")
        return 2
    if args.search_corpus_jsonl:
        search_rows = [json.loads(line) for line in Path(args.search_corpus_jsonl).read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.same_evidence_jsonl:
        same_evidence_rows = [json.loads(line) for line in Path(args.same_evidence_jsonl).read_text(encoding="utf-8").splitlines() if line.strip()]

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
            record = _run_one_unit(
                unit=unit,
                model=args.model,
                catalyst_model=args.catalyst_model,
                baseline_mode=args.baseline_mode,
                client=client,
                pricing=pricing,
                search_rows=search_rows,
                same_evidence_rows=same_evidence_rows,
            )
            if record.get("error_type"):
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
