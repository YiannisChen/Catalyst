"""Single source of truth for database paths + write guard.

H4 owns this module.  Replaces fragile Path(__file__) walks.
"""

from __future__ import annotations

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

DEV_DB = _PROJECT_ROOT / "data" / "catalyst_dev_ws4b.db"
FROZEN_DB = _PROJECT_ROOT / "data" / "catalyst_eval_frozen_v2.db"
APP_DB = _PROJECT_ROOT / "data" / "catalyst_app.db"


def assert_writable(db_path: Path | str) -> None:
    """Raise RuntimeError if *db_path* is frozen or a 0-byte *frozen* shadow."""
    resolved = Path(db_path).resolve()
    frozen_resolved = FROZEN_DB.resolve()

    if resolved == frozen_resolved:
        raise RuntimeError(
            f"Refusing to write to frozen eval DB: {resolved}\n"
            f"Use the dev DB: {DEV_DB}"
        )

    if resolved.exists() and resolved.stat().st_size == 0 and "frozen" in resolved.name.lower():
        raise RuntimeError(
            f"Refusing to write to 0-byte frozen DB shadow: {resolved}\n"
            f"This file is a stale copy artifact. Delete it and use: {DEV_DB}"
        )
