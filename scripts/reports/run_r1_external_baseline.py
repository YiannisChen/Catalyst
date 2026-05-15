from __future__ import annotations

import argparse
import hashlib
import json
import os
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
    raise NotImplementedError("real grader API call is implemented in Task 3")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run R1 external baseline grader (mock-only local runner).")
    parser.add_argument("--input", required=True, help="Input r1_input_chunks.jsonl")
    parser.add_argument("--output", required=True, help="Output r1_sonnet_grades.jsonl")
    parser.add_argument("--model", required=True, help="Model label for output metadata")
    parser.add_argument("--mock", action="store_true", help="Use deterministic mock grading (local preflight only).")
    parser.add_argument("--real", action="store_true", help="Use cloud real grading mode.")
    parser.add_argument("--base-url", default="https://aihubmix.com/v1", help="Base URL for cloud real grader API.")
    parser.add_argument("--limit", type=int, default=0, help="Optional max number of output rows; 0 means all.")
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
                except NotImplementedError as exc:
                    print(f"[fail] {exc}")
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
