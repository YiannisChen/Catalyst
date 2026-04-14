#!/usr/bin/env python3
"""Step B: build or reuse LanceDB index from prepared SQLite data."""
from __future__ import annotations

import argparse
import sqlite3

from event_case_study import DB_PATH, LANCEDB_PATH, build_index


def main() -> None:
    parser = argparse.ArgumentParser(description="Build/reuse case-study retrieval index.")
    parser.add_argument("--reuse-index", action="store_true", help="Reuse existing LanceDB table if present")
    args = parser.parse_args()

    if not DB_PATH.exists():
        raise RuntimeError(f"DB not found: {DB_PATH}. Run case_prepare_data.py first.")

    conn = sqlite3.connect(str(DB_PATH))
    try:
        table, _embedding_fn, chunks = build_index(conn, reuse_index=args.reuse_index)
    finally:
        conn.close()

    if table is None:
        raise RuntimeError("No clean chunks available; index not built.")

    print(f"DB: {DB_PATH}")
    print(f"LanceDB: {LANCEDB_PATH}")
    print(f"Indexed clean chunks: {len(chunks)}")


if __name__ == "__main__":
    main()
