#!/usr/bin/env python3
"""Read-only M8-A generation coverage audit CLI.

Classifies canonical documents across the inactive candidate build, the
candidate/served FTS indexes, and the served dense index. Production inputs are
opened ``mode=ro&immutable=1``; the audit never writes to the database, never
promotes a generation, and never prints a local absolute path.

Usage:
  audit_v1_1_generation_coverage.py \
      --derivative DB --corpus-manifest-id SHA256 --build-id SHA256 \
      --output report.json [--benchmark-cases cases.jsonl]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT / "packages" / "data-core") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "packages" / "data-core"))

from catalyst_data.canonical.generation_coverage import (  # noqa: E402
    audit_generation_coverage,
)


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _expected_primary_ids(path: Path) -> tuple[str, ...]:
    """Post-build audit input only; read-only and never a selection source."""
    ids: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError("benchmark cases file must contain JSON objects")
        for evidence_id in record.get("expected_primary_evidence") or ():
            if isinstance(evidence_id, str) and evidence_id:
                ids.append(evidence_id)
    return tuple(dict.fromkeys(ids))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--derivative", type=Path, required=True)
    parser.add_argument("--corpus-manifest-id", required=True)
    parser.add_argument("--build-id", default=None)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--benchmark-cases", type=Path, default=None,
        help=(
            "Read-only Stage-1 cases.jsonl used only as a post-build audit "
            "input; it never selects documents."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    derivative = Path(args.derivative).resolve()
    if not derivative.is_file():
        print("derivative DB not found", file=sys.stderr)
        return 2
    expected = (
        _expected_primary_ids(Path(args.benchmark_cases))
        if args.benchmark_cases is not None
        else ()
    )
    uri = f"{derivative.as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        report = audit_generation_coverage(
            conn,
            corpus_manifest_id=args.corpus_manifest_id,
            build_id=args.build_id,
            expected_evidence_ids=expected,
        )
    finally:
        conn.close()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(_canonical_bytes(report.as_dict()))
    print(
        _canonical_bytes(
            {
                "ok": True,
                "report_digest": report.report_digest,
                "corpus_manifest_id": report.corpus_manifest_id,
                "build_id": report.build_id,
                "counts": dict(report.counts),
                "full_text_count": report.full_text_count,
                "row_count": len(report.rows),
            }
        ).decode("utf-8")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
