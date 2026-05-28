from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAIN_SUMMARIES = PROJECT_ROOT / "data/eval_reports/p1_runs/p1_full_calibrated_20260514_155759/summaries"
RETRY_SUMMARIES = PROJECT_ROOT / "data/eval_reports/p1_runs/p1_full_calibrated_retry_g005_20260514_181258/summaries"
OUT_MD = PROJECT_ROOT / "docs/reports/2026-05-15-r0-baseline-addendum.md"

PROFILE_ORDER = ["full", "no_rerank", "no_vector", "degraded"]
PATTERN = re.compile(r"_l1l2_(full|no_rerank|no_vector|degraded)_(g\d{3})_")


def _parse_summary(path: Path) -> dict:
    m = PATTERN.search(path.name)
    if not m:
        raise ValueError(f"cannot parse profile/case from {path.name}")
    profile, case_id = m.groups()
    d = json.loads(path.read_text(encoding="utf-8"))
    case = d.get("case", {})
    judge = d.get("judge", {})
    return {
        "profile": profile,
        "case_id": case_id,
        "expected_status": case.get("expected_status"),
        "output_status": judge.get("output_status"),
        "grounding_rate": judge.get("grounding_rate_field"),
        "should_refuse": bool(case.get("should_refuse") is True),
        "data_coverage_gap": bool(case.get("data_coverage_gap") is True),
    }


def _load_merged_rows() -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    for p in sorted(MAIN_SUMMARIES.glob("*.summary.json")):
        r = _parse_summary(p)
        merged[(r["profile"], r["case_id"])] = r
    for p in sorted(RETRY_SUMMARIES.glob("*.summary.json")):
        r = _parse_summary(p)
        merged[(r["profile"], r["case_id"])] = r
    return [merged[k] for k in sorted(merged, key=lambda x: (PROFILE_ORDER.index(x[0]), x[1]))]


def _status_acc(rows: list[dict]) -> float:
    if not rows:
        return 0.0
    return sum(1 for r in rows if r["expected_status"] == r["output_status"]) / len(rows)


def _trivial_baseline_acc(rows: list[dict]) -> tuple[str, float]:
    expected = [r["expected_status"] for r in rows]
    majority = Counter(expected).most_common(1)[0][0]
    acc = sum(1 for s in expected if s == majority) / len(expected)
    return majority, acc


def _mean_grounding(rows: list[dict]) -> float | None:
    vals = [float(r["grounding_rate"]) for r in rows if r["grounding_rate"] is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def main() -> int:
    rows = _load_merged_rows()
    if len(rows) != 200:
        raise RuntimeError(f"expected 200 merged rows, got {len(rows)}")

    raw_acc = _status_acc(rows)
    majority_label, trivial_acc = _trivial_baseline_acc(rows)

    adjusted_rows = [r for r in rows if not r["data_coverage_gap"]]
    adjusted_acc = _status_acc(adjusted_rows)

    refusal_rate = sum(1 for r in rows if r["output_status"] == "INSUFFICIENT") / len(rows)
    grounding_mean = _mean_grounding(rows)

    lines = [
        "# 2026-05-15 R0 Baseline Addendum",
        "",
        "## Trivial Baseline",
        "",
        f"- Trivial policy: always predict majority expected status `{majority_label}`.",
        f"- Trivial baseline accuracy: `{trivial_acc:.4f}`.",
        "",
        "## Dual-Track Metrics",
        "",
        "This addendum reports both raw performance and coverage-adjusted performance to separate model behavior from known data coverage limits.",
        "",
        "## Raw vs Coverage-Adjusted",
        "",
        f"- Raw status accuracy (all 200 rows): `{raw_acc:.4f}`.",
        f"- Coverage-adjusted status accuracy (exclude `data_coverage_gap=true` rows, n={len(adjusted_rows)}): `{adjusted_acc:.4f}`.",
        "",
        "## Refusal and Grounding Framing",
        "",
        f"- Output refusal proxy (`output_status=INSUFFICIENT`) rate: `{refusal_rate:.4f}`.",
        f"- Mean grounding_rate_field (where available): `{grounding_mean:.4f}`." if grounding_mean is not None else "- Mean grounding_rate_field: `N/A`.",
        "- Interpretation: raw metrics capture end-to-end behavior; coverage-adjusted metrics better isolate model decision quality when known data gaps exist.",
    ]

    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[ok] wrote {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
