"""
Run attribution experiments and generate comparison reports.

Usage:
    python scripts/run_experiments.py --golden-set packages/eval/golden_set/v1.jsonl --output data/eval_reports/

Experiments:
    E2: Single Agent vs MCJ (tests whether Critic improves quality)

Spec reference: Section 5.5 — Evaluation Experiments.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from catalyst_eval.schema.golden_event import GoldenEvent
from catalyst_eval.harness.experiment import compare
from catalyst_eval.metrics import (
    AttributionF1,
    CategoryAccuracy,
    GroundingRate,
    TemporalPrecision,
    ConfidenceCalibration,
)
from catalyst_agents.graph import build_attribution_graph
from catalyst_agents.adapter import make_catalyst_predict


ALL_METRICS = [
    AttributionF1(),
    CategoryAccuracy(),
    GroundingRate(),
    TemporalPrecision(),
    ConfidenceCalibration(),
]


def load_golden_set(path: str) -> list[GoldenEvent]:
    """Load golden events from a JSONL file.

    Args:
        path: Path to the JSONL file. Each line must be a valid GoldenEvent JSON object.

    Returns:
        List of GoldenEvent instances.
    """
    events: list[GoldenEvent] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(GoldenEvent(**json.loads(line)))
    return events


def run_e2_experiment(
    golden_set: list[GoldenEvent],
    llm,
    table=None,
    embedding_fn=None,
    reranker=None,
):
    """E2: Single Agent (Miner→Judge) vs MCJ (Miner→Critic→Judge).

    Tests whether the Critic node improves attribution quality by filtering
    low-relevance evidence before synthesis.

    Args:
        golden_set:   Ground-truth events to evaluate against.
        llm:          LLM client instance (shared across both configurations).
        table:        LanceDB table for retrieval.
        embedding_fn: Callable(str) -> list[float] for embedding queries.
        reranker:     Cross-encoder reranker callable.

    Returns:
        ComparisonReport with MCJ vs Baseline scores.
    """
    missing = []
    if table is None:
        missing.append("a populated LanceDB table")
    if embedding_fn is None:
        missing.append("embedding_fn")
    if reranker is None:
        missing.append("reranker")

    if missing:
        raise ValueError(
            "E2 requires a populated LanceDB table, embedding_fn, and reranker. "
            f"Missing: {', '.join(missing)}. "
            "Current script only supports e2 and expects live retrieval dependencies to be "
            "provided by the caller before running experiments."
        )

    mcj_graph = build_attribution_graph(
        use_critic=True,
        table=table,
        embedding_fn=embedding_fn,
        reranker=reranker,
        llm=llm,
    )
    baseline_graph = build_attribution_graph(
        use_critic=False,
        table=table,
        embedding_fn=embedding_fn,
        reranker=reranker,
        llm=llm,
    )

    return compare(
        configs={
            "MCJ (Miner->Critic->Judge)": make_catalyst_predict(mcj_graph),
            "Baseline (Miner->Judge)": make_catalyst_predict(baseline_graph),
        },
        golden_set=golden_set,
        metrics=ALL_METRICS,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Catalyst attribution experiments")
    parser.add_argument(
        "--golden-set",
        required=True,
        help="Path to golden set JSONL file",
    )
    parser.add_argument(
        "--output",
        default="data/eval_reports/",
        help="Output directory for experiment reports (default: data/eval_reports/)",
    )
    parser.add_argument(
        "--experiment",
        choices=["e2"],
        default=None,
        help="Specific experiment to run. Omit to scaffold only.",
    )
    args = parser.parse_args()

    golden_set = load_golden_set(args.golden_set)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loaded {len(golden_set)} golden events from {args.golden_set}")

    if args.experiment is None:
        print("Note: no --experiment flag specified.")
        print("Full experiments require LLM API keys and a populated LanceDB index.")
        print("This script scaffolds the experiment infrastructure.")
        print(f"Reports will be saved to {output_dir}/")
        print()
        print("Currently supported experiments:")
        print("  --experiment e2   MCJ vs Baseline (Critic ablation)")
        return

    if args.experiment == "e2":
        print("Running E2: MCJ vs Baseline (Critic ablation)...")
        print("WARNING: requires ANTHROPIC_API_KEY and a populated LanceDB table.")

        # Late import so the script remains importable without credentials.
        try:
            import anthropic
            from langchain_anthropic import ChatAnthropic  # type: ignore[import]
        except ImportError as exc:
            raise SystemExit(
                f"Missing dependency for live run: {exc}\n"
                "Install with: pip install anthropic langchain-anthropic"
            ) from exc

        llm = ChatAnthropic(model="claude-sonnet-4-20250514", temperature=0)
        try:
            report = run_e2_experiment(golden_set=golden_set, llm=llm)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc

        md_path = output_dir / "e2_mcj_vs_baseline.md"
        md_path.write_text(report.to_markdown())
        print(f"Report saved to {md_path}")
        print()
        print(report.to_markdown())


if __name__ == "__main__":
    main()
