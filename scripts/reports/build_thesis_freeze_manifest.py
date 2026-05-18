from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_dir(path: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(x for x in path.rglob("*") if x.is_file()):
        rel = p.relative_to(path).as_posix().encode("utf-8")
        h.update(rel)
        h.update(b"\x00")
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        h.update(b"\x00")
    return h.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _git_sha() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        return out
    except Exception:
        return "UNKNOWN"


def build_manifest(
    *,
    main_ablation_json: Path,
    golden_set: Path,
    db_path: Path,
    lancedb_dir: Path,
    model_lock_path: Path,
) -> dict[str, Any]:
    ablation_path = main_ablation_json
    golden_path = golden_set

    for p in (ablation_path, golden_path, db_path, lancedb_dir, model_lock_path):
        if not p.exists():
            raise FileNotFoundError(f"required input not found: {p}")

    ablation = _load_json(ablation_path)
    golden_rows = _load_jsonl(golden_path)
    golden_by_id = {str(r["id"]): r for r in golden_rows}

    allowed_profiles = {"full", "no_vector"}
    per_case = [
        row for row in ablation.get("per_case", [])
        if str(row.get("profile")) in allowed_profiles
    ]

    units: list[dict[str, Any]] = []
    for row in per_case:
        cid = str(row["case_id"])
        profile = str(row["profile"])
        g = golden_by_id.get(cid, {})
        query = g.get("query_override") or f"Why did {g.get('ticker', cid)} move on {g.get('trade_date', '')}?"
        units.append(
            {
                "case_id": cid,
                "profile": profile,
                "expected_status": row.get("expected_status"),
                "should_refuse": bool(g.get("should_refuse", False)),
                "ticker": g.get("ticker"),
                "trade_date": g.get("trade_date"),
                "query": query,
            }
        )

    units = sorted(units, key=lambda x: (x["profile"], x["case_id"]))
    failed_case_ids = sorted(
        {
            str(item.get("case_id"))
            for item in ablation.get("failed_cases", [])
            if str(item.get("profile")) in allowed_profiles
        }
    )

    manifest_core = {
        "freeze_id": f"thesis-freeze-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "code_git_sha": _git_sha(),
        "main_run_tag": "final_rerun_20260517_131820_g2_risky_rerun",
        "profiles": ["full", "no_vector"],
        "effective_n": len(units),
        "failed_case_ids": failed_case_ids,
        "main_ablation_json": str(ablation_path),
        "golden_set": str(golden_path),
        "db_path": str(db_path),
        "lancedb_dir": str(lancedb_dir),
        "model_lock_path": str(model_lock_path),
        "frozen_units": units,
    }

    manifest_core["input_sha256"] = {
        "main_ablation_json": _sha256_file(ablation_path),
        "golden_set": _sha256_file(golden_path),
        "db_path": _sha256_file(db_path),
        "lancedb_dir": _sha256_dir(lancedb_dir),
        "model_lock_path": _sha256_file(model_lock_path),
    }

    manifest_core["prompt_contract"] = {
        "direct_prompt_has_expected_status": False,
        "direct_prompt_template_version": "v2_no_label_leak",
    }
    manifest_core["audit_contract_version"] = "v2"
    return manifest_core


def main() -> int:
    parser = argparse.ArgumentParser(description="Build thesis freeze manifest for Catalyst main comparison.")
    parser.add_argument("--main-ablation-json", required=True)
    parser.add_argument("--golden-set", required=True)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--lancedb-dir", required=True)
    parser.add_argument("--model-lock-path", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    output_path = Path(args.output)
    manifest_core = build_manifest(
        main_ablation_json=Path(args.main_ablation_json),
        golden_set=Path(args.golden_set),
        db_path=Path(args.db_path),
        lancedb_dir=Path(args.lancedb_dir),
        model_lock_path=Path(args.model_lock_path),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest_core, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[ok] wrote manifest: {output_path}")
    print(f"[ok] effective_n={manifest_core['effective_n']} failed_case_ids={manifest_core['failed_case_ids']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
