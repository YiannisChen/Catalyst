#!/usr/bin/env python3
"""Tier-A mcj_full reproducibility rerun (offline / mocked LLM). Outputs rerun_* artifacts only.

Invoked from repo root. Does not modify packages/. See T-15 in the P0 construction plan.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(description="Tier-A mcj_full rerun against frozen DB.")
    parser.add_argument("--golden-set", default="packages/eval/golden_set/v1_2_p0_set.jsonl")
    parser.add_argument("--db", default="data/catalyst_eval_frozen.db")
    parser.add_argument("--baseline-mcj", default="data/eval_reports/20260503_154053_mcj_full.json")
    parser.add_argument("--report-dir", default="data/eval_reports")
    parser.add_argument("--trace-dir", default="data/traces")
    args = parser.parse_args()

    root = _repo_root()
    sys.path.insert(0, str(root / "packages" / "eval"))
    sys.path.insert(0, str(root / "packages" / "agents"))

    import os

    os.chdir(root)

    from scripts.run_frozen_eval import (
        _build_fixtures,
        _cost_latency_block,
        _render_config_markdown,
        _run_mcj_cases,
        _should_refuse_hit_rate,
        _status_accuracy,
    )
    from catalyst_eval.harness.frozen_eval import (
        CURRENT_THRESHOLDS,
        SCHEMA_VERSION,
        calibrate_thresholds,
        load_jsonl,
    )

    golden = Path(args.golden_set)
    frozen_db = Path(args.db)
    baseline_path = Path(args.baseline_mcj)
    report_dir = Path(args.report_dir)
    trace_dest = Path(args.trace_dir)

    baseline = json.loads(baseline_path.read_text())
    pinned_header = baseline["header"]

    rerun_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    workspace_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(root), text=True
    ).strip()

    cases = load_jsonl(golden)
    fixtures = _build_fixtures(cases, frozen_db)
    trace_tmp = Path(tempfile.mkdtemp(prefix="catalyst_tier_a_"))
    trace_db = trace_tmp / "trace_runs.db"
    if trace_db.exists():
        trace_db.unlink()

    mcj_rows, _raw, observations = _run_mcj_cases(
        cases,
        fixtures=fixtures,
        frozen_db=frozen_db,
        trace_db=trace_db,
        trace_dir=trace_tmp,
    )
    calibration = calibrate_thresholds(observations, current=CURRENT_THRESHOLDS)

    for row in mcj_rows:
        run_id = row["run_id"]
        src = trace_tmp / f"{run_id}.json"
        dst = trace_dest / f"rerun_{run_id}.json"
        shutil.copy2(src, dst)

    mcj_report = {
        "schema_version": SCHEMA_VERSION,
        "header": pinned_header,
        "config_name": "mcj_full",
        "tier_a_rerun": {
            "rerun_ts": rerun_ts,
            "workspace_git_sha_at_rerun": workspace_sha,
            "pinned_code_git_sha": pinned_header.get("code_git_sha"),
            "baseline_artifact": str(baseline_path.relative_to(root)),
        },
        "aggregate": {
            "status_accuracy": round(_status_accuracy(mcj_rows), 4),
            "should_refuse_hit_rate": round(_should_refuse_hit_rate(mcj_rows), 4),
            **_cost_latency_block(mcj_rows),
        },
        "threshold_calibration": calibration,
        "per_case": mcj_rows,
        "tier_a_diff": _diff_statuses(baseline["per_case"], mcj_rows),
    }

    out_json = report_dir / f"rerun_{rerun_ts}_mcj_full.json"
    out_md = report_dir / f"rerun_{rerun_ts}_mcj_full.md"
    out_json.write_text(json.dumps(mcj_report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # Reuse markdown renderer shape (header from mcj_report for frozen_ts display).
    slim = {
        "header": mcj_report["header"],
        "aggregate": mcj_report["aggregate"],
        "per_case": mcj_report["per_case"],
    }
    out_md.write_text(_render_config_markdown("mcj_full (tier_a_rerun)", slim), encoding="utf-8")

    print(json.dumps({"rerun_json": str(out_json), "rerun_md": str(out_md)}, indent=2))
    if mcj_report["tier_a_diff"]["status_mismatches"]:
        print("TIER_A_STATUS_MISMATCH", file=sys.stderr)
        return 1
    return 0


def _diff_statuses(
    baseline_rows: list[dict[str, object]],
    rerun_rows: list[dict[str, object]],
) -> dict[str, object]:
    base = {row["case_id"]: row["output_status"] for row in baseline_rows}
    new = {row["case_id"]: row["output_status"] for row in rerun_rows}
    mismatches = [
        {"case_id": cid, "baseline": base[cid], "rerun": new[cid]}
        for cid in sorted(base.keys())
        if base.get(cid) != new.get(cid)
    ]
    dist_base: dict[str, int] = {}
    dist_new: dict[str, int] = {}
    for _cid, st in base.items():
        dist_base[str(st)] = dist_base.get(str(st), 0) + 1
    for _cid, st in new.items():
        dist_new[str(st)] = dist_new.get(str(st), 0) + 1
    return {
        "status_mismatches": mismatches,
        "output_status_distribution_baseline": dist_base,
        "output_status_distribution_rerun": dist_new,
    }


if __name__ == "__main__":
    raise SystemExit(main())
