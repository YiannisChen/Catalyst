from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any


_PROFILE_PAT = re.compile(r"_l1l2_(full|no_rerank|no_vector|degraded)_(g\d{3})_")
_L2_PAT = re.compile(r"^(?P<base>.+)::l2s(?P<idx>\d{4})$")


class _SentenceSplitter:
    def __init__(self) -> None:
        self._tokenizer = None
        self._regex = re.compile(r"(?<=[.!?])\s+")

    def split(self, content_md: str) -> list[str]:
        text = (content_md or "").strip()
        if not text:
            return []

        if self._tokenizer is None:
            try:
                from nltk.tokenize.punkt import PunktTokenizer

                self._tokenizer = PunktTokenizer("english")
            except Exception:
                self._tokenizer = False

        if self._tokenizer:
            try:
                return [s.strip() for s in self._tokenizer.tokenize(text) if s and s.strip()]
            except Exception:
                pass

        return [s.strip() for s in self._regex.split(text) if s and s.strip()]


def _load_summary(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _iter_summary_paths(summaries_dir: Path, retry_summaries_dir: Path | None = None) -> list[Path]:
    merged: dict[tuple[str, str], Path] = {}

    for p in sorted(summaries_dir.glob("*.summary.json")):
        m = _PROFILE_PAT.search(p.name)
        if not m:
            continue
        profile, case_id = m.groups()
        merged[(profile, case_id)] = p

    if retry_summaries_dir is not None and retry_summaries_dir.exists():
        for p in sorted(retry_summaries_dir.glob("*.summary.json")):
            m = _PROFILE_PAT.search(p.name)
            if not m:
                continue
            profile, case_id = m.groups()
            merged[(profile, case_id)] = p

    return [merged[k] for k in sorted(merged)]


def _load_asset_content(conn: sqlite3.Connection, asset_id: str) -> str | None:
    row = conn.execute(
        "SELECT content_md FROM clean_assets WHERE asset_id = ? LIMIT 1",
        (asset_id,),
    ).fetchone()
    if not row:
        return None
    return row[0]


def _resolve_chunk_text(
    *,
    conn: sqlite3.Connection,
    splitter: _SentenceSplitter,
    chunk_id: str,
) -> tuple[str, bool]:
    """Return (chunk_text, fallback_to_full_content)."""
    m = _L2_PAT.match(chunk_id)
    if not m:
        content = _load_asset_content(conn, chunk_id) or ""
        return content, False

    base_asset_id = m.group("base")
    sentence_index = int(m.group("idx"))

    parent_content = _load_asset_content(conn, base_asset_id)
    if not parent_content:
        return "", True

    sentences = splitter.split(parent_content)
    # l2s0001 is 1-based index.
    if 1 <= sentence_index <= len(sentences):
        sentence = sentences[sentence_index - 1].strip()
        if sentence:
            return sentence, False

    return parent_content, True


def build_rows(summary_paths: list[Path], db_path: Path | str, case_ids: set[str], profile: str = "full") -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    splitter = _SentenceSplitter()
    rows: list[dict[str, Any]] = []

    try:
        for sp in summary_paths:
            summary = _load_summary(sp)
            case = summary.get("case", {})
            case_id = case.get("id")
            if case_id not in case_ids:
                continue

            m = _PROFILE_PAT.search(sp.name)
            row_profile = m.group(1) if m else profile
            if row_profile != profile:
                continue

            chunks: list[dict[str, Any]] = []
            for score in (summary.get("critic", {}).get("all_graded_scores", []) or []):
                chunk_id = str(score.get("chunk_id", ""))
                if not chunk_id:
                    continue
                chunk_text, fallback = _resolve_chunk_text(conn=conn, splitter=splitter, chunk_id=chunk_id)
                chunks.append(
                    {
                        "chunk_id": chunk_id,
                        "chunk_text": chunk_text,
                        "relevance": float(score.get("relevance", 0.0) or 0.0),
                        "category": str(score.get("category", "")),
                        "fallback_to_full_content": fallback,
                    }
                )

            rows.append(
                {
                    "case_id": case_id,
                    "ticker": case.get("ticker"),
                    "trade_date": case.get("trade_date"),
                    "price_move_pct": case.get("price_move_pct"),
                    "profile": row_profile,
                    "chunks": chunks,
                }
            )
    finally:
        conn.close()

    return rows


def _parse_case_ids(case_ids_csv: str) -> set[str]:
    return {c.strip() for c in case_ids_csv.split(",") if c.strip()}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build fixed-case external baseline input chunks JSONL from frozen summaries.")
    parser.add_argument("--summaries-dir", required=True)
    parser.add_argument("--retry-summaries-dir", required=False)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--case-ids", required=True, help="Comma-separated case ids")
    parser.add_argument("--profile", default="full")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    summary_paths = _iter_summary_paths(
        summaries_dir=Path(args.summaries_dir),
        retry_summaries_dir=Path(args.retry_summaries_dir) if args.retry_summaries_dir else None,
    )
    rows = build_rows(
        summary_paths=summary_paths,
        db_path=Path(args.db_path),
        case_ids=_parse_case_ids(args.case_ids),
        profile=args.profile,
    )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""), encoding="utf-8")

    print(f"[ok] output={out_path}")
    print(f"[ok] rows={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
