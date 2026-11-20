"""Reject new Ruff violations while the existing lint backlog is retired."""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "scripts" / "ruff_baseline.json"


def main() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "packages", "scripts", "deploy/monitor", "--output-format", "json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        print(result.stderr, file=sys.stderr)
        return result.returncode
    actual = Counter(issue["code"] for issue in json.loads(result.stdout))
    allowed = json.loads(BASELINE.read_text())
    increases = {code: count for code, count in actual.items() if count > allowed.get(code, 0)}
    if increases:
        for code, count in sorted(increases.items()):
            print(f"{code}: {count} violations (baseline {allowed.get(code, 0)})", file=sys.stderr)
        return 1
    print(f"Ruff: no rule count increased ({sum(actual.values())} existing violations).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
