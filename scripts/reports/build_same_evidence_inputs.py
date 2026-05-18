from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def build_same_evidence_inputs(manifest_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    excluded = 0
    reasons: dict[str, int] = {}
    for u in manifest.get("frozen_units") or []:
        chunks = list(u.get("evidence_chunks") or [])
        if not chunks:
            excluded += 1
            reasons["no_evidence_chunks"] = reasons.get("no_evidence_chunks", 0) + 1
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
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    rows, meta = build_same_evidence_inputs(Path(args.freeze_manifest))
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
