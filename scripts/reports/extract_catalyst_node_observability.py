from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def extract_trace_node_usage(trace_payload: dict[str, Any]) -> list[dict[str, Any]]:
    agg: dict[str, dict[str, float]] = defaultdict(lambda: {
        "latency_ms_sum": 0.0,
        "input_tokens_sum": 0.0,
        "output_tokens_sum": 0.0,
        "cost_usd_sum": 0.0,
    })
    for event in trace_payload.get("events", []):
        node = str(event.get("node", "unknown"))
        agg[node]["latency_ms_sum"] += float(event.get("latency_ms", 0.0) or 0.0)
        agg[node]["input_tokens_sum"] += float(event.get("input_tokens", 0.0) or 0.0)
        agg[node]["output_tokens_sum"] += float(event.get("output_tokens", 0.0) or 0.0)
        agg[node]["cost_usd_sum"] += float(event.get("cost_usd", 0.0) or 0.0)

    rows: list[dict[str, Any]] = []
    for node, d in sorted(agg.items()):
        rows.append({"node": node, **d})
    return rows


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--trace-json", required=True)
    p.add_argument("--out-json", required=True)
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    trace = json.loads(Path(args.trace_json).read_text(encoding="utf-8"))
    rows = extract_trace_node_usage(trace)
    out = Path(args.out_json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[ok] wrote node observability: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
