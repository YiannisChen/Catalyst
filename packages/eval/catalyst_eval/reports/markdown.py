"""
Markdown report renderer for ComparisonReport.

render_comparison(report) -> str produces a GitHub-flavoured Markdown table
suitable for embedding in experiment logs or pull-request comments.

Table format:
    | Config | <metric_1> | ... | Avg Cost ($) | Avg Tokens |
    |--------|-----------|-----|--------------|------------|
    | name   | 0.85      | ... | 0.02         | 3500       |
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from catalyst_eval.harness.experiment import ComparisonReport


def render_comparison(report: "ComparisonReport") -> str:
    """
    Render a ComparisonReport as a Markdown comparison table.

    Parameters
    ----------
    report:
        ComparisonReport produced by catalyst_eval.harness.experiment.compare().

    Returns
    -------
    str — multi-line Markdown table string.
    """
    if not report.results:
        return "_No results to display._\n"

    # Collect ordered metric names from the first config entry.
    first_config = next(iter(report.results))
    metric_names: list[str] = list(report.results[first_config].keys())

    # Build header row.
    header_cols = ["Config"] + metric_names + ["Avg Cost ($)", "Avg Tokens"]
    header_row = "| " + " | ".join(header_cols) + " |"

    # Build separator row.
    separator_row = "| " + " | ".join(["---"] * len(header_cols)) + " |"

    # Build data rows.
    data_rows: list[str] = []
    for config_name in report.results:
        scores = report.results[config_name]
        cost_info = report.costs.get(config_name, {})

        score_cells = [f"{scores.get(m, 0.0):.4f}" for m in metric_names]
        avg_cost = cost_info.get("avg_cost_usd", 0.0)
        avg_tokens = cost_info.get("avg_tokens", 0.0)

        row_cols = [config_name] + score_cells + [f"{avg_cost:.4f}", f"{avg_tokens:.0f}"]
        data_rows.append("| " + " | ".join(row_cols) + " |")

    lines = [header_row, separator_row] + data_rows
    return "\n".join(lines) + "\n"
