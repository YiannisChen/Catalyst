from __future__ import annotations

import json
from pathlib import Path


def build_validated_cases(src: Path, dst: Path) -> None:
    rows = json.loads(src.read_text(encoding="utf-8"))
    out = []
    for r in rows:
        row = dict(r)
        if row.get("id") == "h001" and row.get("ticker") == "APPL":
            row["ticker"] = "AAPL"
            row["failure_criteria"] = (
                "System normalizes APPL silently or returns attribution instead of explicit insufficiency/refusal."
            )
        out.append(row)
    dst.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--src", required=True)
    parser.add_argument("--dst", required=True)
    args = parser.parse_args()

    build_validated_cases(Path(args.src), Path(args.dst))
