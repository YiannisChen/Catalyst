from __future__ import annotations

from typing import Any


def _pct(value: Any, places: int = 2) -> str:
    return "n/a" if value is None else f"{value:+.{places}f}%"
