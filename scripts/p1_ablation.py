#!/usr/bin/env python3
"""Run P1 ablation matrix and aggregate thesis-ready results.

Matrix unit = (profile, case_id).
Each run calls scripts/p1_trace_report.py and consumes its summary.json.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GOLDEN_SET = PROJECT_ROOT / "packages" / "eval" / "golden_set" / "v1_2_p0_set.jsonl"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "catalyst_eval_frozen_v2.db"
DEFAULT_LANCEDB_DIR = PROJECT_ROOT / "data" / "lancedb_gold" / "eval_frozen"
DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "eval_reports"
TRACE_SCRIPT = PROJECT_ROOT / "scripts" / "p1_trace_report.py"


@dataclass(frozen=True)
class Profile:
    name: str
    reranker_mode: str
    query_embedder_mode: str
    model: str
    note: str


PROFILES: dict[str, Profile] = {
    "full": Profile(
        name="full",
        reranker_mode="auto",
        query_embedder_mode="bge",
        model="gemini-2.5-flash-nothink",
        note="Full pipeline: bge query embedding + cross-encoder reranker.",
    ),
    "no_rerank": Profile(
        name="no_rerank",
        reranker_mode="off",
        query_embedder_mode="bge",
        model="gemini-2.5-flash-nothink",
        note="Ablation: remove reranker, keep real vector embedding.",
    ),
    "no_vector": Profile(
        name="no_vector",
        reranker_mode="auto",
        query_embedder_mode="deterministic",
        model="gemini-2.5-flash-nothink",
        note="Ablation: deterministic query embedding (vector path degraded), keep reranker.",
    ),
    "degraded": Profile(
        name="degraded",
        reranker_mode="off",
        query_embedder_mode="deterministic",
        model="gemini-2.5-flash-nothink",
        note="Lowest-cost demo mode: no reranker + deterministic embedding.",
    ),
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run P1 ablation matrix and aggregate results.")
    parser.add_argument("--golden-set", default=str(DEFAULT_GOLDEN_SET))
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--lancedb-dir", default=str(DEFAULT_LANCEDB_DIR))
    parser.add_argument(
        "--lancedb-dir-variants",
        default="",
        help=(
            "Optional comma-separated variants in form name=path. "
            "Example: l1l2=data/lancedb_gold/eval_frozen,l1=data/lancedb_gold/eval_frozen_l1_only"
        ),
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument(
        "--profiles",
        default="full,no_rerank,no_vector,degraded",
        help=f"Comma-separated profile keys. Choices: {','.join(PROFILES.keys())}",
    )
    parser.add_argument(
        "--case-ids",
        default="",
        help="Comma-separated case ids. Empty means all rows in --golden-set.",
    )
    parser.add_argument("--provider", default="aihubmix")
    parser.add_argument("--base-url", default="https://aihubmix.com/v1")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--window-days", type=int, default=3)
    parser.add_argument("--tag", default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _load_cases(path: Path, wanted_case_ids: set[str] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if wanted_case_ids is not None and row["id"] not in wanted_case_ids:
            continue
        rows.append(row)
    if wanted_case_ids is not None:
        found = {row["id"] for row in rows}
        missing = sorted(wanted_case_ids - found)
        if missing:
            raise ValueError(f"case ids not found in golden set: {missing}")
    return rows


def _run_one(
    *,
    profile: Profile,
    index_variant: str,
    lancedb_dir: str,
    case_id: str,
    run_tag: str,
    args: argparse.Namespace,
    out_dir: Path,
) -> Path:
    per_run_tag = f"{run_tag}_{index_variant}_{profile.name}_{case_id}"
    cmd = [
        sys.executable,
        str(TRACE_SCRIPT),
        "--case-id",
        case_id,
        "--provider",
        args.provider,
        "--model",
        profile.model,
        "--base-url",
        args.base_url,
        "--db",
        args.db,
        "--lancedb-dir",
        lancedb_dir,
        "--reranker-mode",
        profile.reranker_mode,
        "--query-embedder-mode",
        profile.query_embedder_mode,
        "--temperature",
        str(args.temperature),
        "--max-output-tokens",
        str(args.max_output_tokens),
        "--window-days",
        str(args.window_days),
        "--out-dir",
        str(out_dir),
        "--tag",
        per_run_tag,
    ]
    subprocess.run(cmd, check=True)
    return out_dir / f"{per_run_tag}_{case_id}_p1_trace.summary.json"


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def _aggregate(profile_rows: list[dict[str, Any]]) -> dict[str, Any]:
    status_hits = [
        1.0 if row["judge"]["output_status"] == row["case"]["expected_status"] else 0.0
        for row in profile_rows
    ]
    should_refuse_rows = [row for row in profile_rows if bool(row["case"].get("should_refuse"))]
    should_refuse_hits = [
        1.0 if row["judge"]["output_status"] == "INSUFFICIENT" else 0.0
        for row in should_refuse_rows
    ]

    retrieved_l2_ratio = [
        _safe_div(
            float(row["retrieval"]["retrieved_counts"]["l2"]),
            float(row["retrieval"]["retrieved_counts"]["total"]),
        )
        for row in profile_rows
    ]
    reranked_l2_ratio = [
        _safe_div(
            float(row["retrieval"]["reranked_counts"]["l2"]),
            float(row["retrieval"]["reranked_counts"]["total"]),
        )
        for row in profile_rows
    ]

    return {
        "num_cases": len(profile_rows),
        "status_accuracy": _avg(status_hits),
        "should_refuse_hit_rate": _avg(should_refuse_hits),
        "avg_attribution_f1": _avg([float(row["metrics"]["attribution_f1"]) for row in profile_rows]),
        "avg_category_accuracy": _avg([float(row["metrics"]["category_accuracy"]) for row in profile_rows]),
        "avg_grounding_rate": _avg([float(row["metrics"]["grounding_rate"]) for row in profile_rows]),
        "avg_temporal_precision": _avg([float(row["metrics"]["temporal_precision"]) for row in profile_rows]),
        "avg_confidence_calibration": _avg([float(row["metrics"]["confidence_calibration"]) for row in profile_rows]),
        "avg_latency_ms": _avg([float(row["cost_and_trace"]["total_latency_ms"]) for row in profile_rows]),
        "avg_total_tokens": _avg([float(row["cost_and_trace"]["total_tokens"]) for row in profile_rows]),
        "avg_total_cost_usd": _avg([float(row["cost_and_trace"]["total_cost_usd"]) for row in profile_rows]),
        "avg_retrieved_l2_ratio": _avg(retrieved_l2_ratio),
        "avg_reranked_l2_ratio": _avg(reranked_l2_ratio),
        "avg_critic_magnitude_coverage": _avg(
            [float(row["critic"]["magnitude_coverage"] or 0.0) for row in profile_rows]
        ),
        "avg_citation_precision": _avg([float(row["citations"]["citation_precision"]) for row in profile_rows]),
    }


def _render_markdown(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# P1 Ablation Report ({payload['run_tag']})")
    lines.append("")
    lines.append("## Matrix")
    lines.append("")
    lines.append("| index_variant | profile | reranker | query_embedder | model | note |")
    lines.append("|---|---|---|---|---|---|")
    for profile in payload["profiles"]:
        lines.append(
            f"| {profile['index_variant']} | {profile['name']} | {profile['reranker_mode']} | {profile['query_embedder_mode']} | "
            f"{profile['model']} | {profile['note']} |"
        )
    lines.append("")
    lines.append("## Aggregate")
    lines.append("")
    lines.append(
        "| index_variant | profile | cases | status_acc | refuse_hit | F1 | grounding | latency_ms | cost_usd | l2_retrieved | l2_reranked |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in payload["aggregate"]:
        lines.append(
            f"| {row['index_variant']} | {row['profile']} | {row['num_cases']} | {row['status_accuracy']:.3f} | "
            f"{row['should_refuse_hit_rate']:.3f} | {row['avg_attribution_f1']:.3f} | "
            f"{row['avg_grounding_rate']:.3f} | {row['avg_latency_ms']:.1f} | "
            f"{row['avg_total_cost_usd']:.5f} | {row['avg_retrieved_l2_ratio']:.3f} | {row['avg_reranked_l2_ratio']:.3f} |"
        )
    lines.append("")
    lines.append("## Per Case")
    lines.append("")
    lines.append("| index_variant | profile | case_id | expected | actual | f1 | grounding | retrieved(l1/l2) | reranked(l1/l2) |")
    lines.append("|---|---|---|---|---|---:|---:|---|---|")
    for row in payload["per_case"]:
        rc = row["retrieval"]["retrieved_counts"]
        rr = row["retrieval"]["reranked_counts"]
        lines.append(
            f"| {row['index_variant']} | {row['profile']} | {row['case_id']} | {row['expected_status']} | {row['output_status']} | "
            f"{row['attribution_f1']:.3f} | {row['grounding_rate']:.3f} | "
            f"{rc['l1']}/{rc['l2']} | {rr['l1']}/{rr['l2']} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = _parse_args()
    golden_set_path = Path(args.golden_set)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    profile_names = [name.strip() for name in args.profiles.split(",") if name.strip()]
    invalid = [name for name in profile_names if name not in PROFILES]
    if invalid:
        raise ValueError(f"Invalid profile names: {invalid}. valid={list(PROFILES.keys())}")
    profiles = [PROFILES[name] for name in profile_names]
    index_variants: dict[str, str] = {"l1l2": args.lancedb_dir}
    if args.lancedb_dir_variants.strip():
        parsed: dict[str, str] = {}
        for item in args.lancedb_dir_variants.split(","):
            item = item.strip()
            if not item:
                continue
            if "=" not in item:
                raise ValueError(f"Invalid --lancedb-dir-variants item: {item}. expected name=path")
            name, path = item.split("=", 1)
            name = name.strip()
            path = path.strip()
            if not name or not path:
                raise ValueError(f"Invalid --lancedb-dir-variants item: {item}. expected name=path")
            parsed[name] = path
        if not parsed:
            raise ValueError("--lancedb-dir-variants provided but no valid entries parsed")
        index_variants = parsed

    wanted_case_ids = None
    if args.case_ids.strip():
        wanted_case_ids = {c.strip() for c in args.case_ids.split(",") if c.strip()}
    cases = _load_cases(golden_set_path, wanted_case_ids)
    if not cases:
        raise ValueError("No cases selected.")

    run_tag = args.tag or time.strftime("%Y%m%d_%H%M%S", time.gmtime())

    if args.dry_run:
        print(f"[dry-run] run_tag={run_tag}")
        print(f"[dry-run] profiles={[p.name for p in profiles]}")
        print(f"[dry-run] cases={[row['id'] for row in cases]}")
        print(f"[dry-run] index_variants={index_variants}")
        return 0

    profile_to_rows: dict[tuple[str, str], list[dict[str, Any]]] = {
        (index_name, profile.name): []
        for index_name in index_variants
        for profile in profiles
    }
    per_case_rows: list[dict[str, Any]] = []
    for index_name, lancedb_dir in index_variants.items():
        for profile in profiles:
            for case in cases:
                case_id = case["id"]
                print(f"[run] index_variant={index_name} profile={profile.name} case={case_id}")
                summary_path = _run_one(
                    profile=profile,
                    index_variant=index_name,
                    lancedb_dir=lancedb_dir,
                    case_id=case_id,
                    run_tag=run_tag,
                    args=args,
                    out_dir=out_dir,
                )
                summary = json.loads(summary_path.read_text())
                profile_to_rows[(index_name, profile.name)].append(summary)
                per_case_rows.append(
                    {
                        "index_variant": index_name,
                        "profile": profile.name,
                        "case_id": case_id,
                        "expected_status": case.get("expected_status"),
                        "output_status": summary["judge"]["output_status"],
                        "attribution_f1": float(summary["metrics"]["attribution_f1"]),
                        "grounding_rate": float(summary["metrics"]["grounding_rate"]),
                        "retrieval": summary["retrieval"],
                        "summary_json": str(summary_path),
                    }
                )

    aggregate_rows: list[dict[str, Any]] = []
    profile_defs: list[dict[str, Any]] = []
    for index_name in index_variants:
        for profile in profiles:
            agg = _aggregate(profile_to_rows[(index_name, profile.name)])
            agg["index_variant"] = index_name
            agg["profile"] = profile.name
            aggregate_rows.append(agg)
            profile_defs.append(
                {
                    "index_variant": index_name,
                    "name": profile.name,
                    "reranker_mode": profile.reranker_mode,
                    "query_embedder_mode": profile.query_embedder_mode,
                    "model": profile.model,
                    "note": profile.note,
                }
            )

    payload = {
        "run_tag": run_tag,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "golden_set": str(golden_set_path),
        "db": args.db,
        "lancedb_dir": args.lancedb_dir,
        "index_variants": index_variants,
        "profiles": profile_defs,
        "aggregate": aggregate_rows,
        "per_case": per_case_rows,
    }

    json_path = out_dir / f"{run_tag}_p1_ablation.json"
    md_path = out_dir / f"{run_tag}_p1_ablation.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    md_path.write_text(_render_markdown(payload))
    print(f"[ok] ablation_json={json_path}")
    print(f"[ok] ablation_md={md_path}")
    return 0


def build_matrix(
    *,
    model_profiles: list[str],
    routing_profiles: list[str],
    retrieval_profiles: list[str],
    index_variants: list[str],
    case_ids: list[str],
) -> list[dict[str, str]]:
    primary_routing = routing_profiles[0] if routing_profiles else "R0_single"
    rows: list[dict[str, str]] = []
    for m in model_profiles:
        for retr in retrieval_profiles:
            for idx in index_variants:
                for case_id in case_ids:
                    rows.append(
                        {
                            "model_profile": m,
                            "routing_profile": primary_routing,
                            "retrieval_profile": retr,
                            "index_variant": idx,
                            "case_id": case_id,
                        }
                    )
    return rows


def dry_run_cost_estimate(
    *,
    run_count: int,
    avg_tokens: int,
    token_price_per_1k: float,
    gpu_hourly_usd: float,
    avg_run_sec: float,
) -> dict[str, float]:
    api_total = float(run_count) * (float(avg_tokens) / 1000.0) * float(token_price_per_1k)
    infra_total = float(run_count) * float(gpu_hourly_usd) * (float(avg_run_sec) / 3600.0)
    return {
        "api_total_usd": api_total,
        "infra_total_usd": infra_total,
        "grand_total_usd": api_total + infra_total,
    }


if __name__ == "__main__":
    raise SystemExit(main())
