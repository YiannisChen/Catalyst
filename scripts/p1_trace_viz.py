from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt

PNG_NAMES = ["latency_breakdown.png", "cost_quality_scatter.png", "profile_comparison_bar.png"]
NODES = ["miner", "critic", "judge", "validator", "finalizer"]
PALETTE = {
    "miner": "#4C78A8",
    "critic": "#F58518",
    "judge": "#54A24B",
    "validator": "#E45756",
    "finalizer": "#72B7B2",
    "total": "#4C78A8",
}


def _load_payload(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    aggregate = payload.get("aggregate")
    if isinstance(aggregate, list):
        return [row for row in aggregate if isinstance(row, dict)]
    return []


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _label(row: dict[str, Any]) -> str:
    profile = str(row.get("profile", "unknown"))
    index_variant = str(row.get("index_variant", "na"))
    return f"{profile}\n({index_variant})"


def _style_axes(ax: plt.Axes, *, title: str, xlabel: str, ylabel: str) -> None:
    ax.set_title(title, fontsize=13, fontweight="semibold", pad=10)
    ax.set_xlabel(xlabel, fontsize=11)
    ax.set_ylabel(ylabel, fontsize=11)
    ax.tick_params(axis="x", labelsize=9)
    ax.tick_params(axis="y", labelsize=9)
    ax.grid(axis="y", linestyle="--", linewidth=0.7, alpha=0.45)


def _draw_no_data(path: Path, *, title: str) -> None:
    fig, ax = plt.subplots(figsize=(8.8, 5.4), dpi=220)
    ax.axis("off")
    ax.text(
        0.5,
        0.56,
        "No data available",
        ha="center",
        va="center",
        fontsize=18,
        color="#4D4D4D",
        fontweight="semibold",
    )
    ax.text(0.5, 0.42, title, ha="center", va="center", fontsize=11, color="#666666")
    fig.tight_layout()
    fig.savefig(path, dpi=220)
    plt.close(fig)


def _render_latency(rows: list[dict[str, Any]], out_path: Path) -> None:
    labels: list[str] = []
    totals: list[float] = []
    node_values: dict[str, list[float]] = {node: [] for node in NODES}
    has_node_breakdown = False

    for row in rows:
        label = _label(row)
        node_map = row.get("node_latency_ms") if isinstance(row.get("node_latency_ms"), dict) else {}
        per_node = {node: _as_float(node_map.get(node)) or 0.0 for node in NODES}
        row_total = _as_float(row.get("avg_latency_ms"))
        if row_total is None:
            row_total = sum(per_node.values())
        if row_total is None or row_total <= 0:
            continue

        if sum(per_node.values()) > 0:
            has_node_breakdown = True

        labels.append(label)
        totals.append(row_total)
        for node in NODES:
            node_values[node].append(per_node[node])

    if not labels:
        _draw_no_data(out_path, title="Latency Breakdown")
        return

    fig, ax = plt.subplots(figsize=(10.2, 5.8), dpi=220)
    x = list(range(len(labels)))

    if has_node_breakdown:
        bottom = [0.0] * len(labels)
        for node in NODES:
            vals = node_values[node]
            ax.bar(
                x,
                vals,
                bottom=bottom,
                color=PALETTE[node],
                edgecolor="white",
                linewidth=0.5,
                label=node,
            )
            bottom = [b + v for b, v in zip(bottom, vals)]
    else:
        ax.bar(x, totals, color=PALETTE["total"], edgecolor="white", linewidth=0.5, label="total")

    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    _style_axes(ax, title="Latency Breakdown by Profile", xlabel="Profile / Index", ylabel="Latency (ms)")
    ax.legend(frameon=False, fontsize=9, ncols=3, loc="upper left")
    fig.tight_layout()
    fig.savefig(out_path, dpi=220)
    plt.close(fig)


def _render_scatter(rows: list[dict[str, Any]], out_path: Path) -> None:
    points: list[tuple[str, float, float]] = []
    for row in rows:
        cost = _as_float(row.get("avg_total_cost_usd"))
        quality = _as_float(row.get("status_accuracy"))
        if quality is None:
            quality = _as_float(row.get("avg_grounding_rate"))
        if cost is None or quality is None:
            continue
        points.append((_label(row), cost, quality))

    if not points:
        _draw_no_data(out_path, title="Cost vs Quality")
        return

    fig, ax = plt.subplots(figsize=(9.8, 5.8), dpi=220)
    xs = [p[1] for p in points]
    ys = [p[2] for p in points]
    labels = [p[0] for p in points]

    ax.scatter(xs, ys, s=88, color="#4C78A8", alpha=0.9, edgecolor="white", linewidth=0.9)
    for label, x_val, y_val in zip(labels, xs, ys):
        ax.annotate(label.replace("\n", " "), (x_val, y_val), textcoords="offset points", xytext=(5, 5), fontsize=8)

    _style_axes(ax, title="Cost-Quality Tradeoff", xlabel="Average Total Cost (USD)", ylabel="Status Accuracy")
    ax.set_ylim(max(0.0, min(ys) - 0.08), min(1.0, max(ys) + 0.08))
    fig.tight_layout()
    fig.savefig(out_path, dpi=220)
    plt.close(fig)


def _render_profile_bar(rows: list[dict[str, Any]], out_path: Path) -> None:
    labels: list[str] = []
    status_vals: list[float] = []
    grounding_vals: list[float] = []

    for row in rows:
        status = _as_float(row.get("status_accuracy"))
        grounding = _as_float(row.get("avg_grounding_rate"))
        if status is None and grounding is None:
            continue
        labels.append(_label(row))
        status_vals.append(status if status is not None else 0.0)
        grounding_vals.append(grounding if grounding is not None else 0.0)

    if not labels:
        _draw_no_data(out_path, title="Profile Comparison")
        return

    fig, ax = plt.subplots(figsize=(10.4, 5.9), dpi=220)
    x = list(range(len(labels)))
    width = 0.36
    ax.bar([i - width / 2 for i in x], status_vals, width=width, label="status_accuracy", color="#4C78A8")
    ax.bar([i + width / 2 for i in x], grounding_vals, width=width, label="avg_grounding_rate", color="#72B7B2")

    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    _style_axes(ax, title="Quality Metrics by Profile", xlabel="Profile / Index", ylabel="Score")
    ax.set_ylim(0.0, 1.0)
    ax.legend(frameon=False, fontsize=9)
    fig.tight_layout()
    fig.savefig(out_path, dpi=220)
    plt.close(fig)


def render_pngs(input_json: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = _load_payload(Path(input_json))
    rows = _rows(payload)

    _render_latency(rows, out_dir / PNG_NAMES[0])
    _render_scatter(rows, out_dir / PNG_NAMES[1])
    _render_profile_bar(rows, out_dir / PNG_NAMES[2])
