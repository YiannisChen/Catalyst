from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAIN_SUMMARIES = PROJECT_ROOT / "data/eval_reports/p1_runs/p1_full_calibrated_20260514_155759/summaries"
RETRY_SUMMARIES = PROJECT_ROOT / "data/eval_reports/p1_runs/p1_full_calibrated_retry_g005_20260514_181258/summaries"
OUT_CSV = PROJECT_ROOT / "docs/reports/2026-05-15-catalyst-error-taxonomy.csv"
OUT_REPORT = PROJECT_ROOT / "docs/reports/2026-05-15-catalyst-full-debug-analysis.md"

PROFILE_ORDER = ["full", "no_rerank", "no_vector", "degraded"]
PATTERN = re.compile(r"_l1l2_(full|no_rerank|no_vector|degraded)_(g\d{3})_")


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _top_relevance(critic: dict) -> float:
    vals: list[float] = []
    for row in critic.get("all_graded_scores", []) or []:
        try:
            vals.append(float(row.get("relevance", 0.0) or 0.0))
        except Exception:
            vals.append(0.0)
    return max(vals) if vals else 0.0


def _parse_summary(path: Path) -> dict:
    m = PATTERN.search(path.name)
    if not m:
        raise ValueError(f"cannot parse profile/case from {path.name}")
    profile, case_id = m.groups()
    s = _load_json(path)
    case = s.get("case", {})
    critic = s.get("critic", {})
    retrieval = s.get("retrieval", {})
    judge = s.get("judge", {})
    return {
        "profile": profile,
        "case_id": case_id,
        "expected_status": case.get("expected_status"),
        "output_status": judge.get("output_status"),
        "data_coverage_gap": case.get("data_coverage_gap") is True,
        "judge_validation_error": judge.get("validation_error"),
        "critic_sufficiency": critic.get("sufficiency"),
        "critic_next_action": critic.get("next_action"),
        "critic_all_graded_count": int(critic.get("all_graded_count", 0) or 0),
        "critic_below_threshold_count": int(critic.get("below_threshold_count", 0) or 0),
        "critic_top_relevance": round(_top_relevance(critic), 4),
        "retrieval_graded_total": int((retrieval.get("graded_counts", {}) or {}).get("total", 0) or 0),
        "retrieval_reranked_total": int((retrieval.get("reranked_counts", {}) or {}).get("total", 0) or 0),
        "retrieval_graded_counts": json.dumps(retrieval.get("graded_counts", {}), ensure_ascii=False, sort_keys=True),
        "retrieval_reranked_counts": json.dumps(retrieval.get("reranked_counts", {}), ensure_ascii=False, sort_keys=True),
        "judge_grounding_rate_field": judge.get("grounding_rate_field"),
        "summary_path": str(path),
    }


def _load_merged_rows() -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    for p in sorted(MAIN_SUMMARIES.glob("*.summary.json")):
        row = _parse_summary(p)
        merged[(row["profile"], row["case_id"])] = row
    for p in sorted(RETRY_SUMMARIES.glob("*.summary.json")):
        row = _parse_summary(p)
        merged[(row["profile"], row["case_id"])] = row
    return [merged[k] for k in sorted(merged, key=lambda x: (PROFILE_ORDER.index(x[0]), x[1]))]


def _classify(row: dict, full_correct_by_case: dict[str, bool]) -> str:
    # 1) data_coverage_gap == True AND output_status == INSUFFICIENT
    if row["data_coverage_gap"] and row["output_status"] == "INSUFFICIENT":
        return "DATA_COVERAGE_GAP"
    # 2) judge.validation_error non-empty
    if row["judge_validation_error"]:
        return "VALIDATOR_FINALIZER"
    # 3) expected_status in {PARTIAL, INSUFFICIENT} AND output_status == SUFFICIENT
    if row["expected_status"] in {"PARTIAL", "INSUFFICIENT"} and row["output_status"] == "SUFFICIENT":
        return "GOLDEN_LABEL_AMBIGUITY"
    # 4) profile == no_rerank AND same case_id is correct in full profile
    if row["profile"] == "no_rerank" and full_correct_by_case.get(row["case_id"], False):
        return "RETRIEVAL_RERANK"
    # 5) critic.sufficiency in {partial, insufficient} AND graded_total < 2 AND top_relevance >= 0.8
    if (
        row["critic_sufficiency"] in {"partial", "insufficient"}
        and row["retrieval_graded_total"] < 2
        and row["critic_top_relevance"] >= 0.8
    ):
        return "CRITIC_GATE"
    # 6) remaining mismatches
    return "MINER_COVERAGE"


def _write_csv(rows: list[dict]) -> Counter:
    mismatches = [r for r in rows if r["expected_status"] != r["output_status"]]
    full_correct_by_case = {
        r["case_id"]: (r["expected_status"] == r["output_status"])
        for r in rows
        if r["profile"] == "full"
    }

    out_rows: list[dict] = []
    counts: Counter = Counter()
    for row in mismatches:
        fp = _classify(row, full_correct_by_case)
        counts[fp] += 1
        out = dict(row)
        out["first_failure_point"] = fp
        out_rows.append(out)

    fields = [
        "profile",
        "case_id",
        "expected_status",
        "output_status",
        "first_failure_point",
        "critic_all_graded_count",
        "critic_below_threshold_count",
        "critic_top_relevance",
        "critic_sufficiency",
        "critic_next_action",
        "retrieval_graded_total",
        "retrieval_reranked_total",
        "retrieval_graded_counts",
        "retrieval_reranked_counts",
        "judge_validation_error",
        "judge_grounding_rate_field",
        "data_coverage_gap",
        "summary_path",
    ]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out_rows)

    return counts


def _refresh_report_taxonomy_table(counts: Counter) -> None:
    text = OUT_REPORT.read_text(encoding="utf-8")
    total = sum(counts.values())
    lines = [
        "## Failure taxonomy（按首责层）",
        "",
        "| first_failure_point | count | share |",
        "|---|---:|---:|",
    ]
    for k, n in counts.most_common():
        share = (n / total) if total else 0.0
        lines.append(f"| {k} | {n} | {share:.1%} |")
    lines.append("")
    lines.append("逐 case 证据链详见 `2026-05-15-catalyst-error-taxonomy.csv`。")

    block = "\n".join(lines)
    new_text, n = re.subn(
        r"## Failure taxonomy（按首责层）\n.*?逐 case 证据链详见 `2026-05-15-catalyst-error-taxonomy\.csv`。",
        block,
        text,
        count=1,
        flags=re.S,
    )
    if n != 1:
        raise RuntimeError("failed to locate taxonomy section in report")
    OUT_REPORT.write_text(new_text, encoding="utf-8")


def main() -> int:
    rows = _load_merged_rows()
    counts = _write_csv(rows)
    _refresh_report_taxonomy_table(counts)
    print(f"[ok] wrote taxonomy csv: {OUT_CSV}")
    print(f"[ok] updated report table: {OUT_REPORT}")
    print(f"[ok] mismatch_count={sum(counts.values())} counts={dict(counts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
