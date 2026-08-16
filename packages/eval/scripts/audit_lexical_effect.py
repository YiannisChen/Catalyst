"""Read-only frozen-corpus lexical effect audit (AMEND-5).

Runs without embedding models or vector retrieval.  For every case in the T4
case pack it reports normalized lexical terms, matched count, returned count,
top chunk IDs/dates/source classes and cutoff/ticker violations, then writes
an ignored report under ``data/run_reports/post_import/``.

The frozen DB is opened with an immutable read-only URI; TEMP tables are
connection-scoped and never persisted.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB = REPO_ROOT / "data" / "snapshots" / (
    "catalyst_b2o_7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49.db"
)
DEFAULT_CASE_PACK = (
    REPO_ROOT / "data" / "run_reports" / "post_import" / "t4_wave23_final3" / "case_pack.jsonl"
)
DEFAULT_MANIFEST = "3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--case-pack", type=Path, default=DEFAULT_CASE_PACK)
    parser.add_argument("--manifest-id", default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--use-production-retriever", action="store_true",
                        help="call retrieve_lexical directly (slower on the frozen corpus)")
    args = parser.parse_args()

    from catalyst_eval.post_import.lexical_audit import run_lexical_audit

    output_dir = args.output_dir or (
        REPO_ROOT / "data" / "run_reports" / "post_import" / "amend5_lexical_audit"
    )
    report = run_lexical_audit(
        db_path=args.db,
        case_pack_path=args.case_pack,
        manifest_id=args.manifest_id,
        output_dir=output_dir,
        use_production_retriever=args.use_production_retriever,
    )
    print(json.dumps(
        {
            "schema_version": report["schema_version"],
            "case_count": report["case_count"],
            "all_non_empty": report["all_non_empty"],
            "all_effect_valid": report.get("all_effect_valid"),
            "cutoff_violations_total": report["cutoff_violations_total"],
            "ticker_violations_total": report["ticker_violations_total"],
            "total_latency_ms": report.get("total_latency_ms"),
            "report_path": str(output_dir / "report.json"),
        },
        sort_keys=True,
    ))
    # Exit code is driven by effect_valid, not merely non-empty results.
    if (
        not report.get("all_effect_valid")
        or report["cutoff_violations_total"]
        or report["ticker_violations_total"]
    ):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
