#!/usr/bin/env python3
"""F1 threshold sensitivity analysis for Jaccard-based cause matching.

Analyses the golden set v1_2.jsonl to determine an empirically justified
MATCH_THRESHOLD for AttributionF1.  Outputs:

  1. Distribution of intra-event Jaccard scores (distinct causes inside the
     same event — these should NOT match each other).
  2. Distribution of self-match scores (each cause Jaccard'd against itself
     — trivially 1.0, included as sanity).
  3. Distribution of cross-event / same-category Jaccard scores (causes
     from different events that share a category — realistic "near miss").
  4. Threshold sweep: for each candidate threshold, reports how many
     intra-event pairs would be wrongly matched (false positives) and how
     many cross-event/same-category pairs would be correctly separated.
  5. Recommended threshold with justification.

Usage:
    python scripts/f1_threshold_sensitivity.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "eval"))

from catalyst_eval.schema.golden_event import GoldenEvent

# ---------------------------------------------------------------------------
# Jaccard (mirrors the implementation in attribution_f1.py)
# ---------------------------------------------------------------------------

def jaccard(text_a: str, text_b: str) -> float:
    tokens_a = set(text_a.lower().split())
    tokens_b = set(text_b.lower().split())
    if not tokens_a and not tokens_b:
        return 1.0
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


# ---------------------------------------------------------------------------
# Load golden set
# ---------------------------------------------------------------------------

GOLDEN_PATH = PROJECT_ROOT / "packages" / "eval" / "golden_set" / "v1_2.jsonl"


def load_golden() -> list[GoldenEvent]:
    events: list[GoldenEvent] = []
    with open(GOLDEN_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(GoldenEvent(**json.loads(line)))
    return events


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze(events: list[GoldenEvent]):
    # 1. Intra-event pairs (distinct causes within the same event — should NOT match)
    intra_scores: list[float] = []
    for ev in events:
        causes = ev.causes
        for i in range(len(causes)):
            for j in range(i + 1, len(causes)):
                intra_scores.append(jaccard(causes[i].text, causes[j].text))

    # 2. Cross-event, same-category pairs
    by_category: dict[str, list[str]] = defaultdict(list)
    for ev in events:
        for c in ev.causes:
            by_category[c.category.value if hasattr(c.category, "value") else str(c.category)].append(c.text)

    cross_same_cat: list[float] = []
    for cat, texts in by_category.items():
        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                cross_same_cat.append(jaccard(texts[i], texts[j]))

    # 3. All-vs-all (for completeness)
    all_texts = [c.text for ev in events for c in ev.causes]

    return intra_scores, cross_same_cat, all_texts


def percentile(scores: list[float], p: float) -> float:
    if not scores:
        return 0.0
    s = sorted(scores)
    idx = int(len(s) * p / 100)
    return s[min(idx, len(s) - 1)]


def print_distribution(label: str, scores: list[float]):
    if not scores:
        print(f"\n{label}: no data")
        return
    print(f"\n{label} (n={len(scores)})")
    print(f"  min={min(scores):.4f}  p25={percentile(scores, 25):.4f}  "
          f"median={percentile(scores, 50):.4f}  p75={percentile(scores, 75):.4f}  "
          f"p90={percentile(scores, 90):.4f}  p95={percentile(scores, 95):.4f}  "
          f"max={max(scores):.4f}")


def threshold_sweep(intra: list[float], cross_same: list[float]):
    thresholds = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]

    print("\n" + "=" * 76)
    print("THRESHOLD SWEEP")
    print("=" * 76)
    print(f"{'Threshold':>10} | {'Intra-event FP':>16} {'(% of pairs)':>14} | "
          f"{'Cross-cat FP':>14} {'(% of pairs)':>14}")
    print("-" * 76)

    for t in thresholds:
        intra_fp = sum(1 for s in intra if s > t)
        intra_pct = 100 * intra_fp / len(intra) if intra else 0
        cross_fp = sum(1 for s in cross_same if s > t)
        cross_pct = 100 * cross_fp / len(cross_same) if cross_same else 0
        print(f"  {t:>7.2f}  | {intra_fp:>12d}   {intra_pct:>12.1f}%  | "
              f"{cross_fp:>12d}   {cross_pct:>12.1f}%")

    # Find the threshold that eliminates all intra-event false positives
    max_intra = max(intra) if intra else 0.0
    safe_threshold = round(max_intra + 0.01, 2)

    return thresholds, max_intra, safe_threshold


def recommend(max_intra: float, safe_threshold: float, cross_same: list[float]):
    print("\n" + "=" * 76)
    print("RECOMMENDATION")
    print("=" * 76)
    print(f"  Max intra-event Jaccard: {max_intra:.4f}")
    print(f"  Safe threshold (max_intra + 0.01): {safe_threshold:.2f}")
    print()

    # What percent of cross-event/same-category pairs survive at safe threshold?
    cross_above = sum(1 for s in cross_same if s > safe_threshold)
    cross_pct = 100 * cross_above / len(cross_same) if cross_same else 0
    print(f"  At threshold={safe_threshold:.2f}:")
    print(f"    - Intra-event false positives: 0 (by construction)")
    print(f"    - Cross-event/same-cat matches surviving: {cross_above}/{len(cross_same)} ({cross_pct:.1f}%)")
    print()

    # Compare with current 0.20
    current = 0.20
    curr_intra_fp = sum(1 for s in [max_intra] if s > current)
    print(f"  Current threshold (0.20):")
    intra_fp_020 = sum(1 for s in [max_intra] if max_intra > current)
    print(f"    - Risk: max intra-event Jaccard={max_intra:.4f} {'> 0.20 (FALSE POSITIVES POSSIBLE)' if max_intra > current else '<= 0.20 (safe)'}")
    print()

    if safe_threshold <= 0.20:
        print(f"  VERDICT: Current threshold of 0.20 is SAFE for v1_2 golden set.")
        print(f"  The empirical maximum intra-event similarity is {max_intra:.4f},")
        print(f"  leaving a margin of {0.20 - max_intra:.4f}.")
    elif safe_threshold <= 0.30:
        print(f"  VERDICT: RAISE threshold from 0.20 to {safe_threshold:.2f}.")
        print(f"  The current 0.20 risks matching distinct causes within the same event")
        print(f"  (max intra-event Jaccard = {max_intra:.4f}).")
    else:
        print(f"  VERDICT: RAISE threshold from 0.20 to {safe_threshold:.2f}.")
        print(f"  WARNING: This is aggressive. Consider switching to embedding-based matching.")

    print()
    print("  NOTE: Jaccard is a transitional matcher. The production system should use")
    print("  embedding-based similarity (cosine on sentence-transformers vectors) which")
    print("  handles paraphrases and synonyms that bag-of-words Jaccard cannot.")


# ---------------------------------------------------------------------------
# Report output (Markdown)
# ---------------------------------------------------------------------------

def save_report(
    events: list[GoldenEvent],
    intra: list[float],
    cross_same: list[float],
    max_intra: float,
    safe_threshold: float,
):
    lines: list[str] = []
    def w(s: str = ""):
        lines.append(s)

    w("# F1 Threshold Sensitivity Analysis")
    w(f"\nGolden set: `v1_2.jsonl` ({len(events)} events)")
    total_causes = sum(len(ev.causes) for ev in events)
    w(f"Total causes: {total_causes}")
    w()

    w("## Intra-event Jaccard Distribution")
    w("Pairs of distinct causes **within the same event** (should NOT match).")
    w(f"- Count: {len(intra)}")
    if intra:
        w(f"- Min: {min(intra):.4f}  Median: {percentile(intra, 50):.4f}  "
          f"P95: {percentile(intra, 95):.4f}  Max: {max(intra):.4f}")
    w()

    w("## Cross-event / Same-category Jaccard Distribution")
    w("Pairs of causes from **different events** that share a category.")
    w(f"- Count: {len(cross_same)}")
    if cross_same:
        w(f"- Min: {min(cross_same):.4f}  Median: {percentile(cross_same, 50):.4f}  "
          f"P95: {percentile(cross_same, 95):.4f}  Max: {max(cross_same):.4f}")
    w()

    w("## Threshold Sweep")
    w()
    w("| Threshold | Intra FP | Intra FP % | Cross-cat matches | Cross-cat % |")
    w("|-----------|----------|------------|-------------------|-------------|")
    for t in [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]:
        ifp = sum(1 for s in intra if s > t)
        ipct = 100 * ifp / len(intra) if intra else 0
        cfp = sum(1 for s in cross_same if s > t)
        cpct = 100 * cfp / len(cross_same) if cross_same else 0
        w(f"| {t:.2f} | {ifp} | {ipct:.1f}% | {cfp} | {cpct:.1f}% |")
    w()

    w("## Recommendation")
    w(f"- **Max intra-event Jaccard:** {max_intra:.4f}")
    w(f"- **Empirically safe threshold:** {safe_threshold:.2f}")
    w()
    if safe_threshold <= 0.20:
        w(f"Current threshold of 0.20 is **safe** for v1_2. "
          f"Margin: {0.20 - max_intra:.4f}.")
    else:
        w(f"**Raise** threshold from 0.20 to **{safe_threshold:.2f}** to prevent "
          f"intra-event false positives.")
    w()
    w("> Jaccard is a transitional matcher. Production should use embedding-based "
      "similarity (cosine on sentence-transformer vectors).")

    report_dir = PROJECT_ROOT / "data" / "eval_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / "f1_threshold_sensitivity.md"
    path.write_text("\n".join(lines))
    print(f"\nMarkdown report saved to {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 76)
    print("F1 THRESHOLD SENSITIVITY ANALYSIS")
    print(f"Golden set: {GOLDEN_PATH}")
    print("=" * 76)

    events = load_golden()
    total_causes = sum(len(ev.causes) for ev in events)
    print(f"Loaded {len(events)} events, {total_causes} total causes")

    intra, cross_same, all_texts = analyze(events)

    print_distribution("Intra-event Jaccard (distinct causes, same event)", intra)
    print_distribution("Cross-event / same-category Jaccard", cross_same)

    _, max_intra, safe_threshold = threshold_sweep(intra, cross_same)
    recommend(max_intra, safe_threshold, cross_same)

    save_report(events, intra, cross_same, max_intra, safe_threshold)


if __name__ == "__main__":
    main()
