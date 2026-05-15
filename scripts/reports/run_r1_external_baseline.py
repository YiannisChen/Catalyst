from __future__ import annotations

import argparse
import hashlib
import json
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


def _flatten_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in rows:
        case_id = str(row.get("case_id", ""))
        if not case_id:
            continue

        chunks = row.get("chunks")
        if isinstance(chunks, list):
            for chunk in chunks:
                chunk_id = str(chunk.get("chunk_id", ""))
                if not chunk_id:
                    continue
                out.append({"case_id": case_id, "chunk_id": chunk_id})
            continue

        chunk_id = str(row.get("chunk_id", ""))
        if chunk_id:
            out.append({"case_id": case_id, "chunk_id": chunk_id})
    return out


def _mock_relevance(chunk_id: str) -> float:
    digest = hashlib.sha256(chunk_id.encode("utf-8")).hexdigest()
    value = int(digest[:12], 16)
    scale = float(16**12 - 1)
    return round(value / scale, 6)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run R1 external baseline grader (mock-only local runner).")
    parser.add_argument("--input", required=True, help="Input r1_input_chunks.jsonl")
    parser.add_argument("--output", required=True, help="Output r1_sonnet_grades.jsonl")
    parser.add_argument("--model", required=True, help="Model label for output metadata")
    parser.add_argument("--mock", action="store_true", default=True, help="Use deterministic mock grading (default).")
    parser.add_argument("--limit", type=int, default=0, help="Optional max number of output rows; 0 means all.")
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        print(f"[fail] input not found: {input_path}")
        return 2

    if not args.mock:
        print("[fail] non-mock mode is disabled for local runner; use --mock and run real grading on cloud only")
        return 3

    rows = _flatten_rows(_load_jsonl(input_path))
    if not rows:
        print(f"[fail] no valid chunk rows found in input: {input_path}")
        return 4

    if args.limit > 0:
        rows = rows[: args.limit]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        for row in rows:
            out = {
                "case_id": row["case_id"],
                "chunk_id": row["chunk_id"],
                "relevance": _mock_relevance(row["chunk_id"]),
                "model": args.model,
                "mode": "mock",
            }
            f.write(json.dumps(out, ensure_ascii=False) + "\n")

    print(f"[ok] wrote {len(rows)} mock grades to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
