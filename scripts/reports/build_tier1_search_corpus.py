from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def build_tier1_search_corpus(manifest_path: Path) -> list[dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    units = manifest.get("frozen_units") or []
    rows: list[dict[str, Any]] = []
    for u in units:
        candidates = [
            {
                "chunk_id": f"{u.get('case_id')}:{u.get('profile')}:polygon_news:0001",
                "source_rank": 0,
                "source": "polygon_news",
                "content_md": f"News evidence for {u.get('ticker')} on {u.get('trade_date')}",
                "score": 1.0,
            },
            {
                "chunk_id": f"{u.get('case_id')}:{u.get('profile')}:fmp_fundamentals:0001",
                "source_rank": 1,
                "source": "fmp_fundamentals",
                "content_md": f"Fundamentals snapshot for {u.get('ticker')} on {u.get('trade_date')}",
                "score": 0.9,
            },
        ]
        candidates.sort(key=lambda c: (-float(c.get("score", 0.0)), int(c.get("source_rank", 9999)), str(c.get("chunk_id", ""))))
        rows.append(
            {
                "case_id": u.get("case_id"),
                "profile": u.get("profile"),
                "ticker": u.get("ticker"),
                "trade_date": u.get("trade_date"),
                "query": u.get("query"),
                "search_candidates": candidates,
            }
        )
    rows.sort(key=lambda r: (str(r.get("case_id")), str(r.get("profile"))))
    return rows


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build deterministic tier1 search corpus from freeze manifest")
    p.add_argument("--freeze-manifest", required=True)
    p.add_argument("--output", required=True)
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    rows = build_tier1_search_corpus(Path(args.freeze_manifest))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"[ok] wrote tier1 corpus: {out}")
    print(f"[ok] n_rows={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
