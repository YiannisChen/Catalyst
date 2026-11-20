"""Local-only support functions for deploy_sandbox_check."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

from catalyst_data.storage.runtime_paths import runtime_data_dir


def daily_data_root() -> Path:
    """Resolve the target checkout's daily root, preserving the legacy default."""
    if not os.environ.get("CATALYST_RUNTIME_DATA_DIR", "").strip():
        return Path("/opt/catalyst/repo/data/m9")
    return runtime_data_dir(Path("/opt/catalyst/repo"), os.environ)


def backup(plan_file: Path, out: Path) -> None:
    plan = json.loads(plan_file.read_text())
    for row in plan["stores"]:
        src = Path(row["path"])
        if not row["exists"]:
            continue
        side = "archive" if "/archive/" in str(src) else "daily"
        # Preserve relative subdirectories, notably every minute month store.
        base = Path("/opt/catalyst/archive") if side == "archive" else daily_data_root()
        try:
            relative = src.relative_to(base)
        except ValueError:
            relative = Path(src.name)
        dest = out / side / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(f"file:{src.resolve()}?mode=ro", uri=True) as source:
            with sqlite3.connect(dest) as target:
                source.backup(target)
        digest = hashlib.sha256(dest.read_bytes()).hexdigest()
        print(f"{src} -> {dest} sha256={digest}")


def idempotence(result_file: Path) -> None:
    data = json.loads(result_file.read_text())
    changed = [row["path"] for row in data["stores"] if row["changed"]]
    if changed:
        raise SystemExit("second apply changed schema: " + ", ".join(changed))
    print(f"PASS: {len(data['stores'])} stores, zero schema differences")


if __name__ == "__main__":
    command = sys.argv[1]
    if command == "backup":
        backup(Path(sys.argv[2]), Path(sys.argv[3]))
    elif command == "idempotence":
        idempotence(Path(sys.argv[2]))
    else:
        raise SystemExit(f"unknown command: {command}")
