"""Freeze the deterministic 12-case lexical baseline for a promoted DB."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from catalyst_eval.probes.lexical_baseline import freeze_lexical_baseline


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--universe-manifest-id", required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--corpus-manifest-id", required=True)
    parser.add_argument("--probe-report", required=True)
    parser.add_argument("--postbuild-report", required=True)
    parser.add_argument("--case-pack", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True)
    try:
        baseline_id, body = freeze_lexical_baseline(
            conn,
            universe_manifest_id=args.universe_manifest_id,
            snapshot_id=args.snapshot_id,
            corpus_manifest_id=args.corpus_manifest_id,
            probe_report_path=Path(args.probe_report),
            postbuild_readiness_report_path=Path(args.postbuild_report),
            case_pack_path=Path(args.case_pack),
            output_path=Path(args.output),
        )
    finally:
        conn.close()
    print(
        {
            "status": "SUCCEEDED",
            "baseline_id": baseline_id,
            "output": str(Path(args.output).resolve()),
            "case_count": body["case_count"],
            "look_ahead_count": body["look_ahead_count"],
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
