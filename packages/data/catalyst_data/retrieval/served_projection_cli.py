"""Prebuild or reuse the served projection sidecar."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from catalyst_data.retrieval.served_projection import open_or_build_served_projection
from catalyst_data.storage.connect import make_readonly_factory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--dir", required=True, type=Path)
    args = parser.parse_args()
    args.dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    handle, reused = open_or_build_served_projection(
        make_readonly_factory(args.db), projection_dir=args.dir
    )
    print(json.dumps({
        "build_id": handle.build_id,
        "path": str(handle.path),
        "reused": reused,
        "seconds": round(time.monotonic() - started, 3),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
