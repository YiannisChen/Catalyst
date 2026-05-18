from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def aggregate_runtime_stats(summary_paths: list[Path]) -> dict[str, Any]:
    lat: list[float] = []
    tok: list[float] = []
    cost: list[float] = []
    for p in summary_paths:
        d = json.loads(p.read_text(encoding="utf-8"))
        runtime = d.get("runtime", d)
        if runtime.get("latency_ms") is not None:
            lat.append(float(runtime["latency_ms"]))
        if runtime.get("total_tokens") is not None:
            tok.append(float(runtime["total_tokens"]))
        if runtime.get("total_cost_usd") is not None:
            cost.append(float(runtime["total_cost_usd"]))
    n = len(summary_paths) if summary_paths else 1
    return {
        "avg_latency_ms": (sum(lat) / len(lat)) if lat else None,
        "avg_total_tokens": (sum(tok) / len(tok)) if tok else None,
        "avg_total_cost_usd": (sum(cost) / len(cost)) if cost else None,
        "n_summaries": len(summary_paths),
    }


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Aggregate Catalyst runtime stats from summary files")
    p.add_argument("--summaries-glob", required=True)
    p.add_argument("--output-json", required=True)
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    paths = sorted(Path().glob(args.summaries_glob))
    stats = aggregate_runtime_stats(paths)
    out = Path(args.output_json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[ok] wrote catalyst runtime stats: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
