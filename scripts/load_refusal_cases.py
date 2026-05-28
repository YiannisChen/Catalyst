from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_refusal_cases(validated_path: str) -> list[dict[str, Any]]:
    rows = json.loads(Path(validated_path).read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("validated refusal file must be a list")
    return rows
