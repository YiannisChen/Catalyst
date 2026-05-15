from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _flatten_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        case_id = str(row.get("case_id", ""))
        if not case_id:
            continue

        ticker = str(row.get("ticker", ""))
        trade_date = str(row.get("trade_date", ""))
        price_move_pct = row.get("price_move_pct", "")

        chunks = row.get("chunks")
        if isinstance(chunks, list):
            for chunk in chunks:
                chunk_id = str(chunk.get("chunk_id", ""))
                if not chunk_id:
                    continue
                out.append(
                    {
                        "case_id": case_id,
                        "chunk_id": chunk_id,
                        "chunk_text": str(chunk.get("chunk_text", "")),
                        "ticker": ticker,
                        "trade_date": trade_date,
                        "price_move_pct": price_move_pct,
                    }
                )
            continue

        chunk_id = str(row.get("chunk_id", ""))
        if chunk_id:
            out.append(
                {
                    "case_id": case_id,
                    "chunk_id": chunk_id,
                    "chunk_text": str(row.get("chunk_text", "")),
                    "ticker": ticker,
                    "trade_date": trade_date,
                    "price_move_pct": price_move_pct,
                }
            )
    return out


def _mock_relevance(chunk_id: str) -> float:
    digest = hashlib.sha256(chunk_id.encode("utf-8")).hexdigest()
    value = int(digest[:12], 16)
    scale = float(16**12 - 1)
    return round(value / scale, 6)


def _grade_chunk_real(
    *,
    api_key: str,
    base_url: str,
    model: str,
    case_id: str,
    chunk_id: str,
    chunk_text: str,
    ticker: str,
    trade_date: str,
    price_move_pct: Any,
) -> float:
    prompt = (
        "You are grading retrieval evidence for a stock move explanation task.\n"
        f"Ticker: {ticker}\n"
        f"Trade date: {trade_date}\n"
        f"Price move pct: {price_move_pct}\n"
        f"Case id: {case_id}\n"
        f"Chunk id: {chunk_id}\n"
        "Task: score how relevant this chunk is for explaining the SPECIFIC price move above.\n"
        "Do not score topical similarity; score explanatory relevance to the exact move.\n"
        "Return STRICT JSON only with this schema: {\"relevance\": <float between 0 and 1>}.\n"
        f"Chunk text:\n{chunk_text}"
    )
    payload = {
        "model": model,
        "max_tokens": 128,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"real grader HTTPError {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"real grader URLError: {exc.reason}") from exc

    response = json.loads(raw)
    content = response.get("content")
    text: str | None = None
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = str(item.get("text", "")).strip()
                if text:
                    break
    if not text and isinstance(response.get("output_text"), str):
        text = response["output_text"].strip()
    if not text and isinstance(response.get("completion"), str):
        text = response["completion"].strip()
    if not text:
        raise ValueError("real grader response missing text content")

    return _parse_relevance_from_response(text)


def _parse_relevance_from_response(text: str) -> float:
    # 1) Try strict JSON first.
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # 2) Fallback: extract JSON from markdown fences or mixed text.
        candidate: str | None = None
        fence_match = re.search(r"```(?:json)?\s*({.*?})\s*```", text, flags=re.S | re.I)
        if fence_match:
            candidate = fence_match.group(1)
        else:
            brace_match = re.search(r"({.*})", text, flags=re.S)
            if brace_match:
                candidate = brace_match.group(1)
        if not candidate:
            raise ValueError("real grader response is not valid JSON") from exc
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as exc2:
            raise ValueError("real grader response is not valid JSON") from exc2

    if not isinstance(data, dict) or "relevance" not in data:
        raise ValueError("real grader JSON must contain `relevance`")
    try:
        score = float(data["relevance"])
    except (TypeError, ValueError) as exc:
        raise ValueError("relevance must be numeric") from exc
    if not (0.0 <= score <= 1.0):
        raise ValueError("relevance out of range [0,1]")
    return round(score, 6)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run R1 external baseline grader (mock-only local runner).")
    parser.add_argument("--input", required=True, help="Input r1_input_chunks.jsonl")
    parser.add_argument("--output", required=True, help="Output r1_sonnet_grades.jsonl")
    parser.add_argument("--model", required=True, help="Model label for output metadata")
    parser.add_argument("--mock", action="store_true", help="Use deterministic mock grading (local preflight only).")
    parser.add_argument("--real", action="store_true", help="Use cloud real grading mode.")
    parser.add_argument("--base-url", default="https://aihubmix.com/v1", help="Base URL for cloud real grader API.")
    parser.add_argument("--limit", type=int, default=0, help="Optional max number of output rows; 0 means all.")
    parser.add_argument("--max-retries", type=int, default=1, help="Retries for each chunk in --real mode on parse/network failure.")
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        print(f"[fail] input not found: {input_path}")
        return 2

    if args.mock and args.real:
        print("[fail] --mock and --real are mutually exclusive")
        return 5
    if not args.mock and not args.real:
        print("[fail] choose one mode: --mock or --real")
        return 6

    rows = _flatten_rows(_load_jsonl(input_path))
    if not rows:
        print(f"[fail] no valid chunk rows found in input: {input_path}")
        return 4

    if args.limit > 0:
        rows = rows[: args.limit]

    if args.real:
        api_key = os.getenv("AIHUBMIX_API_KEY", "").strip()
        if not api_key:
            print("[fail] AIHUBMIX_API_KEY is required for --real mode")
            return 7

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        for row in rows:
            if args.real:
                relevance: float | None = None
                last_error: ValueError | None = None
                for attempt in range(args.max_retries + 1):
                    try:
                        relevance = _grade_chunk_real(
                            api_key=api_key,
                            base_url=args.base_url,
                            model=args.model,
                            case_id=row["case_id"],
                            chunk_id=row["chunk_id"],
                            chunk_text=str(row.get("chunk_text", "")),
                            ticker=str(row.get("ticker", "")),
                            trade_date=str(row.get("trade_date", "")),
                            price_move_pct=row.get("price_move_pct", ""),
                        )
                        break
                    except ValueError as exc:
                        last_error = exc
                        if attempt < args.max_retries:
                            time.sleep(0.4)
                            continue
                if relevance is None:
                    print(f"[fail] chunk_id={row['chunk_id']} error={last_error}")
                    return 8
                out = {
                    "case_id": row["case_id"],
                    "chunk_id": row["chunk_id"],
                    "relevance": float(relevance),
                    "model": args.model,
                    "mode": "real",
                }
                f.write(json.dumps(out, ensure_ascii=False) + "\n")
                continue
            out = {
                "case_id": row["case_id"],
                "chunk_id": row["chunk_id"],
                "relevance": _mock_relevance(row["chunk_id"]),
                "model": args.model,
                "mode": "mock",
            }
            f.write(json.dumps(out, ensure_ascii=False) + "\n")

    mode = "real" if args.real else "mock"
    print(f"[ok] wrote {len(rows)} {mode} grades to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
