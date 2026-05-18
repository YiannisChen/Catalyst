from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def aggregate_consistency(runs: list[list[dict[str, Any]]]) -> dict[str, Any]:
    by_key: dict[tuple[str, str], list[tuple[str, bool]]] = defaultdict(list)
    for rows in runs:
        for row in rows:
            key = (str(row.get("case_id")), str(row.get("profile")))
            by_key[key].append((str(row.get("output_status", "")), bool(row.get("refusal_flag", False))))

    total = len(by_key)
    status_consistent = 0
    refusal_consistent = 0
    transitions: Counter[str] = Counter()
    for key, seq in by_key.items():
        statuses = [x[0] for x in seq]
        refusals = [x[1] for x in seq]
        if len(set(statuses)) == 1:
            status_consistent += 1
        if len(set(refusals)) == 1:
            refusal_consistent += 1
        transitions["->".join(statuses)] += 1

    denom = max(1, total)
    return {
        "status_consistency_rate": status_consistent / denom,
        "refusal_consistency_rate": refusal_consistent / denom,
        "per_case_transition_counts": dict(transitions),
        "n_cases": total,
        "k_runs": len(runs),
    }


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--run-jsonl", action="append", default=[])
    p.add_argument("--out-json", required=True)
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    runs: list[list[dict[str, Any]]] = []
    for p in args.run_jsonl:
        path = Path(p)
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        runs.append(rows)
    out = aggregate_consistency(runs)
    out_path = Path(args.out_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[ok] wrote stability: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
