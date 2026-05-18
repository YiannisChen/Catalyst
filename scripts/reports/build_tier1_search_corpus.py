from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _load_repo_corpus(repo_root: Path) -> list[dict[str, Any]]:
    corpus: list[dict[str, Any]] = []
    for p in sorted((repo_root / "data" / "eval_reports").glob("*_p1_trace.summary.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        chunks = list(((d.get("retrieval") or {}).get("reranked_chunks") or []))
        for c in chunks:
            chunk_id = c.get("chunk_id") or c.get("asset_id")
            content_md = c.get("content_md") or c.get("content")
            source = c.get("source") or c.get("source_type")
            if not chunk_id or not content_md or not source:
                continue
            corpus.append(
                {
                    "chunk_id": str(chunk_id),
                    "source": str(source),
                    "content_md": str(content_md),
                    "source_rank": 0 if str(source) == "polygon_news" else 1,
                }
            )
    if corpus:
        return corpus
    # deterministic fallback corpus (static, non-template text)
    return [
        {
            "chunk_id": "seed:polygon_news:0001",
            "source": "polygon_news",
            "content_md": "Analysts cite risk-off flows after tariff pause headlines and mixed EV demand signals.",
            "source_rank": 0,
        },
        {
            "chunk_id": "seed:fmp_fundamentals:0001",
            "source": "fmp_fundamentals",
            "content_md": "Daily fundamentals snapshot highlights valuation pressure and weaker delivery expectations.",
            "source_rank": 1,
        },
    ]


def _score_candidate(unit: dict[str, Any], cand: dict[str, Any]) -> float:
    q = f"{unit.get('ticker','')} {unit.get('trade_date','')} {unit.get('query','')}"
    qtok = _tokenize(q)
    ctok = _tokenize(str(cand.get("content_md", "")))
    overlap = len(qtok & ctok)
    ticker_bonus = 2 if str(unit.get("ticker", "")).lower() in ctok else 0
    return float(overlap + ticker_bonus)


def _infer_repo_root(manifest_path: Path) -> Path:
    cwd = Path.cwd().resolve()
    for p in [cwd, *cwd.parents]:
        if (p / ".git").exists() or (p / "scripts" / "reports").exists():
            return p
    return manifest_path.resolve().parents[2] if len(manifest_path.resolve().parents) >= 3 else cwd


def build_tier1_search_corpus(manifest_path: Path, repo_root: Path | None = None) -> list[dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    resolved_repo_root = repo_root.resolve() if repo_root is not None else _infer_repo_root(manifest_path)
    corpus = _load_repo_corpus(resolved_repo_root)
    units = manifest.get("frozen_units") or []
    rows: list[dict[str, Any]] = []
    for u in units:
        candidates = []
        for c in corpus:
            score = _score_candidate(u, c)
            if score <= 0:
                continue
            candidates.append({**c, "score": score})
        candidates.sort(key=lambda c: (-float(c.get("score", 0.0)), int(c.get("source_rank", 9999)), str(c.get("chunk_id", ""))))
        candidates = candidates[:5] if candidates else sorted(corpus, key=lambda c: (int(c.get("source_rank", 9999)), str(c.get("chunk_id", ""))))[:2]
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
    p.add_argument("--repo-root", default=None)
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    rows = build_tier1_search_corpus(
        Path(args.freeze_manifest),
        repo_root=Path(args.repo_root).resolve() if args.repo_root else None,
    )
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
