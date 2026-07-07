"""Doctor — single-button operator contract gate for the data-core pipeline.

Runs four audit dimensions, returns structured pass/fail result,
and supports --json output with secret redaction.

Usage:
    from catalyst_data.doctor import doctor
    result = doctor("data/catalyst_dev_ws4b.db", json_output=True)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DOCTOR_VERSION = "1.0.0"


def _redact_secrets(obj: Any) -> Any:
    """Recursively redact API keys and secrets from any object."""
    SECRET_KEY_PATTERNS = ("key", "secret", "token", "password", "api")

    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if any(p in k.lower() for p in SECRET_KEY_PATTERNS):
                result[k] = "[REDACTED]"
            else:
                result[k] = _redact_secrets(v)
        return result
    if isinstance(obj, list):
        return [_redact_secrets(v) for v in obj]
    if isinstance(obj, str):
        lower = obj.lower()
        if "sk-" in lower or "bearer " in lower or "api_key=" in lower:
            return "[REDACTED]"
    return obj


def _unknown_publisher_audit(conn: sqlite3.Connection) -> dict:
    """Detect unknown publishers across all articles."""
    from catalyst_data.source_tier import unknown_publisher_audit
    return unknown_publisher_audit(conn)


def _dry_run_zero_write_assertion(db_path: str) -> dict:
    """Verify that running build_index_records does not write any rows.

    Opens a read-only connection, runs the full dry-run code path,
    then verifies row counts in core tables are unchanged.
    """
    def _row_counts(conn):
        counts = {}
        for table in ("articles", "article_tickers", "raw_assets", "clean_assets",
                       "filings", "filing_documents", "index_state", "index_manifests",
                       "source_checkpoints", "ingestion_runs"):
            try:
                cnt = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                cnt = -1
            counts[table] = cnt
        return counts

    conn_before = sqlite3.connect(db_path)
    before = _row_counts(conn_before)
    conn_before.close()

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA query_only = ON")
    try:
        from catalyst_data.index_builder import build_index_records, index_summary
        records = build_index_records(conn, min_l2_chars=800)
        index_summary(records)
    finally:
        conn.close()

    conn_after = sqlite3.connect(db_path)
    after = _row_counts(conn_after)
    conn_after.close()

    unchanged = before == after
    diffs = {k: (before[k], after[k]) for k in before if before[k] != after[k]}

    return {
        "row_counts_unchanged": unchanged,
        "diffs": diffs,
        "gate_passed": unchanged,
    }


def doctor(
    db_path: str,
    *,
    json_output: bool = False,
) -> dict[str, Any]:
    """Run all four audit gates and return structured result.

    Args:
        db_path: Path to the dev DB.
        json_output: If True, print JSON to stdout before returning.

    Returns:
        dict with gate results, all_gates_passed, and exit_code.
    """
    d = hashlib.sha256()
    with open(db_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            d.update(chunk)
    db_sha = d.hexdigest()

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA query_only = ON")

    gates: dict[str, Any] = {}

    # Gate 1: P0 invariants
    try:
        from catalyst_data.coverage_audit import audit_gate_p0
        gates["gate_p0"] = audit_gate_p0(conn)
    except Exception as exc:
        gates["gate_p0"] = {"error": str(exc), "gate_passed": False}

    # Gate 2: Embed readiness
    try:
        from catalyst_data.coverage_audit import audit_embed_readiness
        gates["embed_readiness"] = audit_embed_readiness(conn)
    except Exception as exc:
        gates["embed_readiness"] = {"error": str(exc), "gate_passed": False}

    # Gate 3: Unknown publishers
    try:
        gates["unknown_publishers"] = _unknown_publisher_audit(conn)
    except Exception as exc:
        gates["unknown_publishers"] = {"error": str(exc), "gate_passed": False, "unknown_publishers": [], "count": 0}

    conn.close()

    # Gate 4: Dry-run zero-write (runs outside the read-only connection)
    try:
        gates["dry_run_zero_write"] = _dry_run_zero_write_assertion(db_path)
    except Exception as exc:
        gates["dry_run_zero_write"] = {"error": str(exc), "gate_passed": False}

    all_passed = all(
        gates[g].get("gate_passed", False)
        for g in gates
    )
    exit_code = 0 if all_passed else 1

    result: dict[str, Any] = {
        "doctor_version": DOCTOR_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_path": os.path.realpath(db_path),
        "db_sha256": db_sha,
        "all_gates_passed": all_passed,
        "gates": gates,
        "exit_code": exit_code,
    }

    if json_output:
        redacted = _redact_secrets(result)
        print(json.dumps(redacted, indent=2, default=str))
    else:
        _print_pretty(result)

    return result


def _print_pretty(result: dict) -> None:
    """Pretty-print the doctor report."""
    print()
    print("=== Doctor Report ===")
    print(f"  DB: {result['db_path']}")
    print(f"  SHA256: {result['db_sha256']}")
    print(f"  Generated: {result['generated_at']}")
    print()

    gates = result.get("gates", {})
    labels = {
        "gate_p0": "Gate P0",
        "embed_readiness": "Embed Readiness",
        "unknown_publishers": "Unknown Publishers",
        "dry_run_zero_write": "Dry-Run Zero-Write",
    }

    passed_count = 0
    for gate_name in ("gate_p0", "embed_readiness", "unknown_publishers", "dry_run_zero_write"):
        gate = gates.get(gate_name, {})
        gate_passed = gate.get("gate_passed", False)
        label = labels.get(gate_name, gate_name)

        if gate_passed:
            passed_count += 1

        status = "[PASS]" if gate_passed else "[FAIL]"
        detail = ""

        if gate_name == "gate_p0":
            p0_total = gate.get("total_checks", 0)
            p0_pass = gate.get("passed_count", 0)
            p0_fail = gate.get("failed_count", 0)
            detail = f" ({p0_pass}/{p0_total} checks)" if p0_total else ""
            if p0_fail:
                failing = gate.get("failing", [])
                detail += f" — failing: {', '.join(failing[:5])}"
        elif gate_name == "embed_readiness":
            er_total = gate.get("total_checks", 0)
            er_pass = gate.get("passed_count", 0)
            detail = f" ({er_pass}/{er_total} checks)" if er_total else ""
        elif gate_name == "unknown_publishers":
            count = gate.get("count", 0)
            detail = f": {count} unknown"
            if count:
                pubs = gate.get("unknown_publishers", [])
                for p in pubs[:5]:
                    detail += f"\n    - {p}"
        elif gate_name == "dry_run_zero_write":
            unchanged = gate.get("row_counts_unchanged", True)
            diffs = gate.get("diffs", {})
            detail = f" (row counts {'unchanged' if unchanged else 'CHANGED'})"
            if diffs:
                for tbl, (b, a) in list(diffs.items())[:3]:
                    detail += f"\n    {tbl}: {b} → {a}"

        print(f"{status} {label}{detail}")

    total = len(gates)
    print()
    print(f"Result: {passed_count}/{total} gates passed — EXIT {result['exit_code']}")
