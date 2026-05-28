#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "eval_reports"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate g013 direct-vs-catalyst dual track report")
    parser.add_argument("--direct-json", required=True)
    parser.add_argument("--catalyst-input", required=True, help="summary.json file or directory containing *_p1_trace.summary.json")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--tag", default=None)
    return parser.parse_args()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _collect_catalyst_rows(path: Path, tag: str | None) -> dict[str, dict[str, Any]]:
    files: list[Path]
    if path.is_file():
        files = [path]
    else:
        files = sorted(path.glob("*_p1_trace.summary.json"))

    selected: dict[str, dict[str, Any]] = {}
    for file_path in files:
        try:
            payload = _load_json(file_path)
        except Exception:
            continue
        run = payload.get("run", {})
        model_id = run.get("model")
        if not isinstance(model_id, str) or not model_id:
            continue
        run_tag = run.get("run_tag")
        if tag and isinstance(run_tag, str) and tag not in run_tag and run_tag != tag:
            continue
        candidate = {
            "summary_path": str(file_path),
            "output_status": payload.get("judge", {}).get("output_status"),
            "grounding_rate": payload.get("metrics", {}).get("grounding_rate"),
            "citation_precision": payload.get("citations", {}).get("citation_precision"),
            "latency_ms": payload.get("cost_and_trace", {}).get("total_latency_ms"),
            "total_cost_usd": payload.get("cost_and_trace", {}).get("total_cost_usd"),
            "run_tag": run_tag,
        }
        prev = selected.get(model_id)
        if prev is None:
            selected[model_id] = candidate
            continue
        prev_tag = prev.get("run_tag")
        # Prefer exact tag match; otherwise keep latest mtime.
        if tag and run_tag == tag and prev_tag != tag:
            selected[model_id] = candidate
        elif file_path.stat().st_mtime > Path(prev["summary_path"]).stat().st_mtime:
            selected[model_id] = candidate

    return selected


def _to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _reliability_verdict(direct: dict[str, Any] | None, catalyst: dict[str, Any] | None) -> bool:
    if direct is None or catalyst is None:
        return False
    direct_status = str(direct.get("status_class") or "")
    direct_reliable = (
        direct_status == "SUFFICIENT"
        and not bool(direct.get("hallucination_flag"))
        and direct_status != "SYSTEM_ERROR"
    )
    output_status = str(catalyst.get("output_status") or "")
    grounding = _to_float(catalyst.get("grounding_rate"))
    citation = _to_float(catalyst.get("citation_precision"))
    catalyst_reliable = output_status == "INSUFFICIENT" or (
        grounding is not None and citation is not None and grounding >= 0.6 and citation >= 0.6
    )
    return bool(catalyst_reliable and not direct_reliable)


def _delta(catalyst_value: Any, direct_value: Any) -> float | None:
    c = _to_float(catalyst_value)
    d = _to_float(direct_value)
    if c is None or d is None:
        return None
    return c - d


def _render_markdown(rows: list[dict[str, Any]], direct_json: str, catalyst_input: str, comparison_mode: str) -> str:
    lines: list[str] = []
    lines.append("# g013 Dual-Track Compare")
    lines.append("")
    lines.append(f"- direct_json: `{direct_json}`")
    lines.append(f"- catalyst_input: `{catalyst_input}`")
    lines.append(f"- comparison_mode: `{comparison_mode}`")
    lines.append("")
    lines.append("| model_id | direct_status | direct_hallucination | direct_latency_ms | direct_cost_usd | catalyst_status | catalyst_grounding | catalyst_citation_precision | catalyst_latency_ms | catalyst_cost_usd | cost_delta | latency_delta | catalyst_better_on_reliability |")
    lines.append("|---|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|")
    for row in rows:
        d = row["direct"]
        c = row["catalyst"]
        v = row["verdict"]
        delta = row["delta"]
        lines.append(
            f"| {row['model_id']} | {d.get('status_class')} | {d.get('hallucination_flag')} | {d.get('latency_ms')} | {d.get('total_cost_usd')} | "
            f"{c.get('output_status')} | {c.get('grounding_rate')} | {c.get('citation_precision')} | {c.get('latency_ms')} | {c.get('total_cost_usd')} | "
            f"{delta.get('cost_delta')} | {delta.get('latency_delta')} | {v.get('catalyst_better_on_reliability')} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = _parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    direct_payload = _load_json(Path(args.direct_json))
    direct_rows = direct_payload.get("per_model", [])
    if not isinstance(direct_rows, list):
        raise ValueError("direct_json missing per_model list")

    catalyst_map = _collect_catalyst_rows(Path(args.catalyst_input), args.tag)
    direct_map: dict[str, dict[str, Any]] = {}
    for row in direct_rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("model_id")
        if isinstance(model_id, str) and model_id:
            direct_map[model_id] = row

    if len(catalyst_map) == 1 and len(direct_map) >= 1:
        comparison_mode = "single_catalyst_vs_multi_direct"
        single_catalyst = next(iter(catalyst_map.values()))
        model_ids = sorted(direct_map.keys())
    else:
        comparison_mode = "model_aligned"
        single_catalyst = None
        model_ids = sorted(set(direct_map.keys()) | set(catalyst_map.keys()))
    rows: list[dict[str, Any]] = []

    for model_id in model_ids:
        direct_row = direct_map.get(model_id)
        catalyst_row = single_catalyst if comparison_mode == "single_catalyst_vs_multi_direct" else catalyst_map.get(model_id)

        direct_block = {
            "missing": direct_row is None,
            "status_class": None if direct_row is None else direct_row.get("status_class"),
            "hallucination_flag": None if direct_row is None else direct_row.get("hallucination_flag"),
            "latency_ms": None if direct_row is None else direct_row.get("latency_ms"),
            "total_cost_usd": None if direct_row is None else direct_row.get("total_cost_usd"),
        }
        catalyst_block = {
            "missing": catalyst_row is None,
            "output_status": None if catalyst_row is None else catalyst_row.get("output_status"),
            "grounding_rate": None if catalyst_row is None else catalyst_row.get("grounding_rate"),
            "citation_precision": None if catalyst_row is None else catalyst_row.get("citation_precision"),
            "latency_ms": None if catalyst_row is None else catalyst_row.get("latency_ms"),
            "total_cost_usd": None if catalyst_row is None else catalyst_row.get("total_cost_usd"),
            "summary_path": None if catalyst_row is None else catalyst_row.get("summary_path"),
        }
        row = {
            "model_id": model_id,
            "direct": direct_block,
            "catalyst": catalyst_block,
            "delta": {
                "cost_delta": _delta(catalyst_block["total_cost_usd"], direct_block["total_cost_usd"]),
                "latency_delta": _delta(catalyst_block["latency_ms"], direct_block["latency_ms"]),
            },
        }
        row["verdict"] = {
            "catalyst_better_on_reliability": _reliability_verdict(direct_row, catalyst_row),
        }
        rows.append(row)

    payload = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "direct_json": str(Path(args.direct_json)),
        "catalyst_input": str(Path(args.catalyst_input)),
        "comparison_mode": comparison_mode,
        "rows": rows,
    }

    if args.tag:
        out_json = out_dir / f"{args.tag}_g013_dual_track_compare.json"
        out_md = out_dir / f"{args.tag}_g013_dual_track_compare.md"
    else:
        out_json = out_dir / "g013_dual_track_compare.json"
        out_md = out_dir / "g013_dual_track_compare.md"
    out_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    out_md.write_text(
        _render_markdown(rows, payload["direct_json"], payload["catalyst_input"], comparison_mode),
        encoding="utf-8",
    )

    print(f"[ok] compare_json={out_json}")
    print(f"[ok] compare_md={out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
