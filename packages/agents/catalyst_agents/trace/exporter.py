"""CLI and helper for exporting a run trace to JSON."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
from typing import Any

from catalyst_data.config import db_path as default_db_path

from catalyst_agents.trace.schema import init_trace_db


def export_run(run_id: str, *, out_path: Path | str, db_path: Path | str | None = None) -> dict[str, Any]:
    target_db = Path(db_path) if db_path is not None else default_db_path()
    conn = sqlite3.connect(str(target_db))
    init_trace_db(conn)
    conn.row_factory = sqlite3.Row

    run_row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
    if run_row is None:
        conn.close()
        raise ValueError(f"Run not found: {run_id}")

    event_rows = conn.execute(
        "SELECT * FROM trace_events WHERE run_id = ? ORDER BY event_seq ASC",
        (run_id,),
    ).fetchall()
    conn.close()

    payload = {
        **dict(run_row),
        "events": [dict(row) for row in event_rows],
    }

    out_file = Path(out_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Export a persisted agent trace to JSON.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    export_run(args.run_id, out_path=args.out)


if __name__ == "__main__":
    main()
