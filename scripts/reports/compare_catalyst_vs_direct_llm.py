from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


RATE_METRICS = {
    "status_accuracy",
    "should_refuse_hit_rate",
    "refusal_precision",
    "refusal_recall",
    "system_error_rate",
}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compare Catalyst vs Direct-LLM on frozen thesis cohort")
    p.add_argument("--freeze-manifest", required=True)
    p.add_argument("--catalyst-ablation-json", required=True)
    p.add_argument("--direct-per-unit-jsonl", required=True)
    p.add_argument("--output-json", required=True)
    p.add_argument("--output-csv", required=True)
    p.add_argument("--output-md", required=True)
    return p.parse_args()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _safe_avg(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _as_rate(numer: int, denom: int) -> float:
    if denom <= 0:
        return 0.0
    return numer / denom


def _compute_metrics(rows: list[dict[str, Any]], *, has_refusal_flag: bool, has_grounding: bool) -> dict[str, Any]:
    n = len(rows)
    status_match = 0
    system_error = 0

    tp = fp = fn = tn = 0

    latencies: list[float] = []
    total_tokens: list[float] = []
    total_cost: list[float] = []
    grounding: list[float] = []

    for row in rows:
        expected = str(row.get("expected_status", "")).upper()
        output = str(row.get("output_status", "")).upper()
        should_refuse = bool(row.get("should_refuse", False))

        if output == expected:
            status_match += 1
        if output == "SYSTEM_ERROR":
            system_error += 1

        if has_refusal_flag:
            pred_refuse = bool(row.get("refusal_flag", False)) or output == "INSUFFICIENT"
        else:
            pred_refuse = output == "INSUFFICIENT"

        if pred_refuse and should_refuse:
            tp += 1
        elif pred_refuse and not should_refuse:
            fp += 1
        elif (not pred_refuse) and should_refuse:
            fn += 1
        else:
            tn += 1

        if row.get("latency_ms") is not None:
            try:
                latencies.append(float(row["latency_ms"]))
            except Exception:
                pass
        if row.get("total_tokens") is not None:
            try:
                total_tokens.append(float(row["total_tokens"]))
            except Exception:
                pass
        if row.get("total_cost_usd") is not None:
            try:
                total_cost.append(float(row["total_cost_usd"]))
            except Exception:
                pass

        if has_grounding:
            val = row.get("grounding_rate")
            if val is not None:
                try:
                    grounding.append(float(val))
                except Exception:
                    pass

    precision = _as_rate(tp, tp + fp)
    recall = _as_rate(tp, tp + fn)
    hit_rate = _as_rate(tp + tn, n)

    return {
        "n": n,
        "status_accuracy": _as_rate(status_match, n),
        "should_refuse_hit_rate": hit_rate,
        "refusal_precision": precision,
        "refusal_recall": recall,
        "system_error_rate": _as_rate(system_error, n),
        "system_error_count": system_error,
        "avg_grounding_rate": _safe_avg(grounding) if has_grounding else None,
        "avg_latency_ms": _safe_avg(latencies),
        "avg_total_tokens": _safe_avg(total_tokens),
        "avg_total_cost_usd": _safe_avg(total_cost),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
    }


def _fmt(v: Any) -> str:
    if v is None:
        return "NA"
    if isinstance(v, float):
        return f"{v:.6f}"
    return str(v)


def _to_pp(v: float | None) -> float | None:
    if v is None:
        return None
    return v * 100.0


def main() -> int:
    args = _parse_args()
    freeze_path = Path(args.freeze_manifest)
    catalyst_path = Path(args.catalyst_ablation_json)
    direct_path = Path(args.direct_per_unit_jsonl)
    out_json = Path(args.output_json)
    out_csv = Path(args.output_csv)
    out_md = Path(args.output_md)

    for p in (freeze_path, catalyst_path, direct_path):
        if not p.exists():
            raise FileNotFoundError(f"required input not found: {p}")

    freeze = _load_json(freeze_path)
    catalyst = _load_json(catalyst_path)
    direct_rows = _load_jsonl(direct_path)

    units = freeze.get("frozen_units", [])
    if not isinstance(units, list) or not units:
        raise ValueError("freeze manifest missing frozen_units")

    expected_map: dict[tuple[str, str], dict[str, Any]] = {
        (str(u["case_id"]), str(u["profile"])): u for u in units
    }

    catalyst_map: dict[tuple[str, str], dict[str, Any]] = {}
    for row in catalyst.get("per_case", []):
        key = (str(row.get("case_id")), str(row.get("profile")))
        if key in expected_map:
            item = dict(row)
            item["expected_status"] = expected_map[key].get("expected_status")
            item["should_refuse"] = bool(expected_map[key].get("should_refuse", False))
            catalyst_map[key] = item

    direct_map: dict[tuple[str, str], dict[str, Any]] = {}
    for row in direct_rows:
        key = (str(row.get("case_id")), str(row.get("profile")))
        if key in expected_map:
            direct_map[key] = row

    keys = sorted(expected_map.keys())

    missing_catalyst = [k for k in keys if k not in catalyst_map]
    missing_direct = [k for k in keys if k not in direct_map]
    if missing_catalyst:
        raise ValueError(f"catalyst missing {len(missing_catalyst)} frozen units, sample={missing_catalyst[:5]}")
    if missing_direct:
        raise ValueError(f"direct baseline missing {len(missing_direct)} frozen units, sample={missing_direct[:5]}")

    catalyst_rows = [catalyst_map[k] for k in keys]
    direct_rows_aligned = [direct_map[k] for k in keys]

    catalyst_metrics = _compute_metrics(catalyst_rows, has_refusal_flag=False, has_grounding=True)
    direct_metrics = _compute_metrics(direct_rows_aligned, has_refusal_flag=True, has_grounding=False)

    metric_names = [
        "status_accuracy",
        "should_refuse_hit_rate",
        "refusal_precision",
        "refusal_recall",
        "system_error_rate",
        "avg_grounding_rate",
        "avg_latency_ms",
        "avg_total_tokens",
        "avg_total_cost_usd",
    ]

    deltas: dict[str, Any] = {}
    for name in metric_names:
        c = catalyst_metrics.get(name)
        d = direct_metrics.get(name)
        if c is None or d is None:
            deltas[name] = None
            continue
        if name in RATE_METRICS:
            deltas[name] = (float(c) - float(d)) * 100.0
        else:
            deltas[name] = float(c) - float(d)

    result = {
        "freeze_id": freeze.get("freeze_id"),
        "main_run_tag": freeze.get("main_run_tag"),
        "cohort_n": len(keys),
        "cohort_profiles": sorted({p for _, p in keys}),
        "catalyst": catalyst_metrics,
        "direct_llm": direct_metrics,
        "delta": deltas,
        "delta_definition": "Catalyst - DirectLLM; rate metrics are percentage points",
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["metric", "catalyst", "direct_llm", "delta"])
        writer.writeheader()
        for name in metric_names:
            writer.writerow(
                {
                    "metric": name,
                    "catalyst": _fmt(catalyst_metrics.get(name)),
                    "direct_llm": _fmt(direct_metrics.get(name)),
                    "delta": _fmt(deltas.get(name)),
                }
            )

    out_md.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 2026-05-18 Catalyst vs Direct-LLM Main Comparison",
        "",
        f"- Freeze ID: `{freeze.get('freeze_id')}`",
        f"- Main run: `{freeze.get('main_run_tag')}`",
        f"- Cohort N: `{len(keys)}`",
        f"- Profiles: `{', '.join(sorted({p for _, p in keys}))}`",
        "",
        "## Main Table",
        "",
        "| Metric | Catalyst | Direct-LLM | Delta (Catalyst - Direct) |",
        "|---|---:|---:|---:|",
    ]
    for name in metric_names:
        lines.append(
            f"| {name} | {_fmt(catalyst_metrics.get(name))} | {_fmt(direct_metrics.get(name))} | {_fmt(deltas.get(name))} |"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- `avg_grounding_rate` for Direct-LLM is `NA` because no grounding field is produced in direct output schema.",
            "- Delta for rate metrics is in percentage points.",
        ]
    )
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"[ok] wrote metrics json: {out_json}")
    print(f"[ok] wrote main table csv: {out_csv}")
    print(f"[ok] wrote markdown report: {out_md}")
    print(f"[ok] cohort_n={len(keys)}")
    print(
        "[ok] key metrics "
        f"status_acc catalyst={catalyst_metrics['status_accuracy']:.4f} direct={direct_metrics['status_accuracy']:.4f} "
        f"delta_pp={deltas['status_accuracy']:.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
