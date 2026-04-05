"""Build LanceDB Gold index from Silver clean_assets.

Reads all non-duplicate rows from the SQLite Silver layer (clean_assets),
embeds content_md using bge-m3, and upserts them into a LanceDB table
'chunks' with a full-text (BM25) index on content_md.

Usage:
    python -m scripts.build_index --db data/dev_assets.db --lancedb data/lancedb/

Requires the optional 'vector' extras:
    pip install 'catalyst-data[vector]'
"""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build LanceDB Gold index from Silver clean_assets.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--db",
        required=True,
        metavar="PATH",
        help="Path to the SQLite Silver database (e.g. data/dev_assets.db).",
    )
    parser.add_argument(
        "--lancedb",
        required=True,
        metavar="DIR",
        help="Directory for the LanceDB Gold store (e.g. data/lancedb/).",
    )
    parser.add_argument(
        "--model",
        default="BAAI/bge-m3",
        metavar="MODEL_ID",
        help="HuggingFace model ID for the embedding model (default: BAAI/bge-m3).",
    )
    args = parser.parse_args()

    try:
        from catalyst_data.storage.lancedb_store import build_index
    except ImportError as exc:
        print(f"[error] Failed to import lancedb_store: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        count = build_index(args.db, args.lancedb, args.model)
    except ImportError as exc:
        print(f"[error] Missing optional dependency: {exc}", file=sys.stderr)
        print(
            "Install with: pip install 'catalyst-data[vector]'",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Indexed {count} chunks into {args.lancedb}")


if __name__ == "__main__":
    main()
