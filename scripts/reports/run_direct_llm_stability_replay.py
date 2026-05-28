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
    for seq in by_key.values():
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
    p.add_argument("--run-artifacts-dir")
    p.add_argument("--freeze-manifest")
    p.add_argument("--model")
    p.add_argument("--provider")
    p.add_argument("--base-url")
    p.add_argument("--k-runs", type=int, default=1)
    p.add_argument("--out-json", required=True)
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    runs: list[list[dict[str, Any]]] = []
    run_sources: list[str] = []

    if args.run_jsonl:
        for p in args.run_jsonl:
            path = Path(p)
            rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
            runs.append(rows)
            run_sources.append(str(path))
    elif args.freeze_manifest:
        if args.run_artifacts_dir:
            base = Path(args.run_artifacts_dir)
            candidates = sorted(base.glob("direct_llm_*_frozen_per_unit*.jsonl"))
            if not candidates:
                candidates = sorted(base.glob("*.jsonl"))
            for path in candidates[: max(1, args.k_runs)]:
                rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
                runs.append(rows)
                run_sources.append(str(path))
        if not runs:
            raise ValueError(
                "--freeze-manifest mode requires real run inputs via --run-jsonl (repeatable) "
                "or --run-artifacts-dir; label replay is not allowed."
            )
    else:
        raise ValueError("provide either --run-jsonl ... or --freeze-manifest with --k-runs")

    if len(runs) < max(1, args.k_runs):
        raise ValueError(f"insufficient real runs: got {len(runs)} but k-runs={args.k_runs}")

    out = aggregate_consistency(runs)
    out["run_sources"] = run_sources
    out_path = Path(args.out_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[ok] wrote stability: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
