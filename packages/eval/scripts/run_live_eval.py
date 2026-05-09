from __future__ import annotations

import argparse
from typing import Any


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run live eval")
    parser.add_argument("--golden-set", required=True)
    parser.add_argument("--llm-seed", type=int, required=True)
    parser.add_argument("--stats-seed", type=int, required=True)
    parser.add_argument("--cost-cap-usd", type=float, required=True)
    return parser.parse_args(argv)


def run_live_cases(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {"status": "not_implemented_yet"}


if __name__ == "__main__":
    parse_args()
