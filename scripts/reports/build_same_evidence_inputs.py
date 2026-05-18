from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _resolve_summary_path(summary_json: str, repo_root: Path) -> Path:
    src = str(summary_json or "").strip()
    if src.startswith("/root/Catalyst/"):
        rel = src[len("/root/Catalyst/") :]
        return repo_root / rel
    p = Path(src)
    if p.is_absolute():
        return p
    return repo_root / src


def _extract_reranked_chunks(summary_payload: dict[str, Any]) -> list[dict[str, Any]]:
    retrieval = summary_payload.get("retrieval") or {}
    chunks = list(retrieval.get("reranked_chunks") or [])
    if chunks:
        return chunks
    assets = list(retrieval.get("reranked_assets") or [])
    normalized: list[dict[str, Any]] = []
    for a in assets:
        normalized.append(
            {
                "chunk_id": a.get("asset_id") or a.get("chunk_id"),
                "content_md": a.get("content_md") or a.get("content"),
                "source": a.get("source_type") or a.get("source"),
            }
        )
    return normalized


def build_same_evidence_inputs(manifest_path: Path, repo_root: Path | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    repo_root = repo_root or Path(__file__).resolve().parents[2]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    excluded = 0
    reasons: dict[str, int] = {}
    for u in manifest.get("frozen_units") or []:
        chunks = list(u.get("evidence_chunks") or [])
        if not chunks and u.get("summary_json"):
            sp = _resolve_summary_path(str(u.get("summary_json")), repo_root=repo_root)
            if sp.exists():
                summary_payload = json.loads(sp.read_text(encoding="utf-8"))
                chunks = _extract_reranked_chunks(summary_payload)
        if not chunks:
            excluded += 1
            reasons["no_reranked_chunks"] = reasons.get("no_reranked_chunks", 0) + 1
            continue
        row = {
            "case_id": u.get("case_id"),
            "profile": u.get("profile"),
            "evidence_chunks": chunks,
            "evidence_chunks_count": len(chunks),
        }
        rows.append(row)
    rows.sort(key=lambda r: (str(r.get("case_id")), str(r.get("profile"))))
    meta = {
        "tier2_eligible_n": len(rows),
        "tier2_excluded_n": excluded,
        "tier2_exclusion_reason_counts": reasons,
    }
    return rows, meta


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build same-evidence direct inputs from freeze manifest")
    p.add_argument("--freeze-manifest", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--meta-output", required=True)
    p.add_argument("--repo-root")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    rows, meta = build_same_evidence_inputs(
        Path(args.freeze_manifest),
        repo_root=Path(args.repo_root) if args.repo_root else None,
    )
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    Path(args.meta_output).write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[ok] wrote same-evidence rows: {out}")
    print(f"[ok] tier2_eligible_n={meta['tier2_eligible_n']} tier2_excluded_n={meta['tier2_excluded_n']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
