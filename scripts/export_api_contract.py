#!/usr/bin/env python3
"""Write the canonical OpenAPI document for the Catalyst API."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(REPO / "packages/api"),
    str(REPO / "packages/agents"),
    str(REPO / "packages/data"),
]


def render_openapi(document: dict) -> str:
    """Return deterministic, pretty JSON with recursively stable key order."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    from catalyst_app.main import create_app

    destination = REPO / "contracts/openapi.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_openapi(create_app().openapi()), encoding="utf-8")


if __name__ == "__main__":
    main()
