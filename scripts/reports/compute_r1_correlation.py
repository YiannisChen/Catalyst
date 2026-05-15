from __future__ import annotations

import argparse
import json
import math
import statistics
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


def _normalize_catalyst_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if "chunks" in row and isinstance(row["chunks"], list):
            case_id = str(row.get("case_id", ""))
            for chunk in row["chunks"]:
                cid = str(chunk.get("chunk_id", ""))
                if not case_id or not cid:
                    continue
                out.append(
                    {
                        "case_id": case_id,
                        "chunk_id": cid,
                        "relevance": float(chunk.get("relevance", 0.0) or 0.0),
                    }
                )
        else:
            case_id = str(row.get("case_id", ""))
            cid = str(row.get("chunk_id", ""))
            if not case_id or not cid:
                continue
            out.append(
                {
                    "case_id": case_id,
                    "chunk_id": cid,
                    "relevance": float(row.get("relevance", 0.0) or 0.0),
                }
            )
    return out


def _normalize_external_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        case_id = str(row.get("case_id", ""))
        cid = str(row.get("chunk_id", ""))
        if not case_id or not cid:
            continue
        out.append(
            {
                "case_id": case_id,
                "chunk_id": cid,
                "relevance": float(row.get("relevance", 0.0) or 0.0),
            }
        )
    return out


def _pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 2 or len(ys) < 2:
        return 0.0
    mx = statistics.mean(xs)
    my = statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mx) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - my) ** 2 for y in ys))
    den = den_x * den_y
    if den == 0:
        return 0.0
    return num / den


def _average_ranks(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda t: t[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j + 2) / 2.0
        for k in range(i, j + 1):
            ranks[indexed[k][0]] = avg_rank
        i = j + 1
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 2 or len(ys) < 2:
        return 0.0
    rx = _average_ranks(xs)
    ry = _average_ranks(ys)
    return _pearson(rx, ry)


def _bucket(v: float) -> str:
    if v < 0.33:
        return "low"
    if v < 0.66:
        return "medium"
    return "high"


def _bucket_agreement(xs: list[float], ys: list[float]) -> float:
    if not xs:
        return 0.0
    agree = sum(1 for x, y in zip(xs, ys) if _bucket(x) == _bucket(y))
    return agree / len(xs)


def compute_metrics(catalyst_rows: list[dict[str, Any]], external_rows: list[dict[str, Any]]) -> dict[str, float]:
    c_map = {(r["case_id"], r["chunk_id"]): float(r["relevance"]) for r in catalyst_rows}
    e_map = {(r["case_id"], r["chunk_id"]): float(r["relevance"]) for r in external_rows}

    keys = sorted(set(c_map.keys()) & set(e_map.keys()))
    if not keys:
        return {"pearson": 0.0, "spearman": 0.0, "bucket_agreement": 0.0, "aligned_pairs": 0}

    xs = [c_map[k] for k in keys]
    ys = [e_map[k] for k in keys]
    return {
        "pearson": _pearson(xs, ys),
        "spearman": _spearman(xs, ys),
        "bucket_agreement": _bucket_agreement(xs, ys),
        "aligned_pairs": len(keys),
    }


def _write_report(path: Path, metrics: dict[str, float], catalyst_input: Path, external_input: Path) -> None:
    lines = [
        "# 2026-05-15 R1 External Baseline Results",
        "",
        "## Status",
        "",
        "Cloud external grades loaded and compared against Catalyst frozen input.",
        "",
        "## Inputs",
        "",
        f"- Catalyst input: `{catalyst_input}`",
        f"- External input: `{external_input}`",
        "",
        "## Metrics",
        "",
        f"- Pearson: `{metrics['pearson']:.4f}`",
        f"- Spearman: `{metrics['spearman']:.4f}`",
        f"- Bucket agreement: `{metrics['bucket_agreement']:.4f}`",
        f"- Aligned pairs: `{metrics['aligned_pairs']}`",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute R1 external baseline correlation metrics.")
    parser.add_argument("--catalyst-input", required=True)
    parser.add_argument("--external-input", required=True)
    parser.add_argument("--output-report", required=True)
    args = parser.parse_args()

    c_path = Path(args.catalyst_input)
    e_path = Path(args.external_input)
    out_path = Path(args.output_report)

    if not c_path.exists():
        print(f"[fail] catalyst input not found: {c_path}")
        return 2
    if not e_path.exists():
        print(f"[fail] external input not found: {e_path}")
        return 3

    c_rows = _normalize_catalyst_rows(_load_jsonl(c_path))
    e_rows = _normalize_external_rows(_load_jsonl(e_path))
    metrics = compute_metrics(c_rows, e_rows)

    if metrics["aligned_pairs"] == 0:
        print("[fail] no aligned (case_id, chunk_id) pairs between catalyst and external inputs")
        return 4

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_report(out_path, metrics, c_path, e_path)
    print(f"[ok] wrote report: {out_path}")
    print(f"[ok] aligned_pairs={metrics['aligned_pairs']} pearson={metrics['pearson']:.4f} spearman={metrics['spearman']:.4f} bucket={metrics['bucket_agreement']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
