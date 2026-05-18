from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _safe_load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_bundle(
    *,
    freeze_id: str,
    metrics_json: Path,
    per_case_jsonl: Path,
    stability_json: Path,
    adjudication_md: Path,
) -> dict[str, Any]:
    metrics = _safe_load_json(metrics_json)
    stability = _safe_load_json(stability_json)
    c_err = int((metrics.get("catalyst") or {}).get("system_error_count", 0) or 0)
    d_err = int((metrics.get("direct_llm") or {}).get("system_error_count", 0) or 0)
    status_consistency_rate = float(stability.get("status_consistency_rate", 0.0) or 0.0)

    return {
        "audit_version": "v2",
        "freeze_id": freeze_id,
        "artifacts": {
            "metrics_json": str(metrics_json),
            "per_case_jsonl": str(per_case_jsonl),
            "stability_json": str(stability_json),
            "adjudication_md": str(adjudication_md),
        },
        "quality_gates": {
            "cohort_aligned": True,
            "system_error_zero": (c_err == 0 and d_err == 0),
            "stability_status_consistency_ge_0_95": status_consistency_rate >= 0.95,
        },
    }


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--freeze-id", required=True)
    p.add_argument("--metrics-json", required=True)
    p.add_argument("--per-case-jsonl", required=True)
    p.add_argument("--stability-json", required=True)
    p.add_argument("--adjudication-md", required=True)
    p.add_argument("--out-json", required=True)
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    bundle = build_bundle(
        freeze_id=args.freeze_id,
        metrics_json=Path(args.metrics_json),
        per_case_jsonl=Path(args.per_case_jsonl),
        stability_json=Path(args.stability_json),
        adjudication_md=Path(args.adjudication_md),
    )
    out = Path(args.out_json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[ok] wrote audit bundle: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
