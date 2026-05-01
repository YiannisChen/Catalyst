#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_REPO_ROOT = _PACKAGE_ROOT.parent.parent
sys.path.insert(0, str(_PACKAGE_ROOT))

from catalyst_data.quality import migrate_databases  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create ingestion run/checkpoint tables and backfill asset quality flags.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_DEFAULT_REPO_ROOT,
        help="Repository root containing data/ (default: current Catalyst repo).",
    )
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    db_paths = [
        repo_root / "data" / "catalyst_dev.db",
        repo_root / "data" / "catalyst_eval_frozen.db",
        repo_root / "data" / "catalyst_demo.db",
    ]

    reports = migrate_databases(db_paths)
    for report in reports:
        rel = report.db_path.relative_to(repo_root)
        print(
            f"{rel} clean_assets={report.clean_assets_count} "
            f"asset_quality_flags={report.asset_quality_flags_count} "
            f"eligible={report.eligible_count} "
            f"ingestion_runs={report.ingestion_runs_count} "
            f"source_checkpoints={report.source_checkpoints_count}"
        )


if __name__ == "__main__":
    main()
