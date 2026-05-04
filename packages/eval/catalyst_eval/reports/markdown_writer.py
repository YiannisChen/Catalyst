"""Markdown writer for frozen comparison artifacts."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from catalyst_eval.reports.json_writer import normalize_comparison_payload


def render_frozen_comparison(payload: dict[str, Any]) -> str:
    normalized = normalize_comparison_payload(payload)
    header = normalized["header"]
    lines = [
        "# Frozen Comparison",
        "",
        f"- frozen_ts: `{header['frozen_ts']}`",
        f"- code_git_sha: `{header['code_git_sha']}`",
        f"- db_sha256: `{header['db_sha256']}`",
    ]
    warning = normalized.get("warning")
    if warning:
        lines.extend(["", f"> {warning}"])
    lines.extend(["", "## Gates", ""])
    for gate, value in normalized["gates"].items():
        lines.append(f"- {gate}: `{value}`")
    lines.extend(["", "## Quality Metrics", ""])
    for config_name, metrics in normalized["quality_metrics"].items():
        lines.append(f"### {config_name}")
        for metric_name, value in metrics.items():
            lines.append(f"- {metric_name}: `{value}`")
        lines.append("")
    if lines[-1] != "":
        lines.append("")
    lines.extend(["## Per Case", "", "| case_id | expected | direct_llm | mcj_full |", "|---|---|---|---|"])
    for row in normalized["per_case"]:
        lines.append(
            f"| {row['case_id']} | {row['expected_status']} | {row['direct_llm']['output_status']} | {row['mcj_full']['output_status']} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_comparison_markdown(payload: dict[str, Any], path: Path | str) -> Path:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_frozen_comparison(payload))
    return out_path
