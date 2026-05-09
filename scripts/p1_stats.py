from __future__ import annotations

from collections import defaultdict
import math
from math import sqrt
from typing import Any

import numpy as np
from scipy.stats import chi2, norm, wilcoxon


STATUS_ORDER = ["SUFFICIENT", "PARTIAL", "INSUFFICIENT"]


def build_status_contingency_3x3(pairs: list[dict[str, Any]]) -> np.ndarray:
    idx = {s: i for i, s in enumerate(STATUS_ORDER)}
    table = np.zeros((3, 3), dtype=int)
    for row in pairs:
        a = idx[row["a_status"]]
        b = idx[row["b_status"]]
        table[a, b] += 1
    return table


def run_bowker(table: np.ndarray) -> dict[str, Any]:
    stat = 0.0
    df = 0
    for i in range(table.shape[0]):
        for j in range(i + 1, table.shape[1]):
            nij = int(table[i, j])
            nji = int(table[j, i])
            denom = nij + nji
            if denom > 0:
                stat += ((nij - nji) ** 2) / denom
                df += 1
    p = float(chi2.sf(stat, df)) if df > 0 else 1.0
    return {
        "method": "bowker",
        "statistic": float(stat),
        "df": int(df),
        "p_value": p,
        "n": int(table.sum()),
    }


def run_stuart_maxwell_or_bowker(table: np.ndarray) -> dict[str, Any]:
    # For this evaluation plan we use Bowker's symmetry test as the 3-class paired test.
    return run_bowker(table)


def run_mcnemar_test(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    n00 = n01 = n10 = n11 = 0
    for row in pairs:
        a = int(row["a_refuse"])
        b = int(row["b_refuse"])
        if a == 0 and b == 0:
            n00 += 1
        elif a == 0 and b == 1:
            n01 += 1
        elif a == 1 and b == 0:
            n10 += 1
        else:
            n11 += 1

    table = np.array([[n00, n01], [n10, n11]])
    discordant = n01 + n10
    exact = discordant < 25
    if discordant == 0:
        stat = 0.0
        p_value = 1.0
    elif exact:
        # Exact two-sided binomial test on discordant pairs.
        k = min(n01, n10)
        # Sum probabilities from 0..k and double for two-sided tail.
        probs = [float(math.comb(discordant, i)) * (0.5 ** discordant) for i in range(0, k + 1)]
        p_value = min(1.0, 2.0 * sum(probs))
        stat = float(min(n01, n10))
    else:
        # Asymptotic McNemar with continuity correction.
        stat = ((abs(n01 - n10) - 1.0) ** 2) / float(discordant)
        p_value = float(chi2.sf(stat, 1))
    return {
        "method": "mcnemar",
        "statistic": float(stat),
        "p_value": float(p_value),
        "exact": bool(exact),
        "n": int(len(pairs)),
        "table": table.tolist(),
    }


def run_wilcoxon_test(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    x = np.array([float(p["a_metric"]) for p in pairs], dtype=float)
    y = np.array([float(p["b_metric"]) for p in pairs], dtype=float)
    diffs = x - y

    if np.allclose(diffs, 0.0):
        stat = 0.0
        p = 1.0
    else:
        w = wilcoxon(x, y, zero_method="wilcox", correction=False)
        stat = float(w.statistic)
        p = float(w.pvalue)

    return {
        "metric": "a_metric_vs_b_metric",
        "method": "wilcoxon",
        "statistic": stat,
        "p_value": p,
        "n": int(len(pairs)),
        "zero_diff_policy": "wilcox",
    }


def compute_cohens_dz(x: np.ndarray, y: np.ndarray) -> float:
    diffs = x - y
    if diffs.size < 2:
        return 0.0
    sd = float(np.std(diffs, ddof=1))
    if sd == 0.0:
        return 0.0
    return float(np.mean(diffs) / sd)


def compute_wilcoxon_r(p_value: float, n: int, mean_diff: float) -> float:
    if n <= 0:
        return 0.0
    if p_value <= 0.0:
        z_abs = 8.0
    else:
        z_abs = float(norm.isf(p_value / 2.0))
    sign = 1.0 if mean_diff >= 0 else -1.0
    z = sign * z_abs
    return float(abs(z) / sqrt(n))


def compute_effect_sizes(pairs: list[dict[str, Any]], wilcoxon_p: float) -> dict[str, float]:
    x = np.array([float(p["a_metric"]) for p in pairs], dtype=float)
    y = np.array([float(p["b_metric"]) for p in pairs], dtype=float)
    mean_diff = float(np.mean(x - y)) if len(pairs) else 0.0
    return {
        "cohens_dz": compute_cohens_dz(x, y),
        "wilcoxon_r": compute_wilcoxon_r(wilcoxon_p, len(pairs), mean_diff),
    }


def stratified_bootstrap_ci(pairs: list[dict[str, Any]], seed: int, iterations: int = 2000) -> dict[str, float]:
    if not pairs:
        return {"metric": "a_minus_b_mean", "lower": 0.0, "upper": 0.0, "iterations": 0}

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in pairs:
        groups[str(row["a_status"])].append(row)

    rng = np.random.default_rng(seed)
    diffs = []
    for _ in range(iterations):
        sampled = []
        for rows in groups.values():
            idx = rng.integers(0, len(rows), len(rows))
            sampled.extend(rows[i] for i in idx)
        delta = np.mean([float(r["a_metric"]) - float(r["b_metric"]) for r in sampled])
        diffs.append(float(delta))

    lo, hi = np.percentile(np.array(diffs, dtype=float), [2.5, 97.5])
    return {
        "metric": "a_minus_b_mean",
        "lower": float(lo),
        "upper": float(hi),
        "iterations": int(iterations),
    }


def holm_correction(p_values: list[float]) -> dict[str, Any]:
    m = len(p_values)
    indexed = sorted(enumerate(p_values), key=lambda x: x[1])
    adjusted = [0.0] * m
    running_max = 0.0
    for rank, (orig_idx, p) in enumerate(indexed, start=1):
        adj = (m - rank + 1) * float(p)
        running_max = max(running_max, adj)
        adjusted[orig_idx] = min(1.0, running_max)
    return {
        "method": "holm",
        "raw_p_values": [float(x) for x in p_values],
        "adjusted_p_values": adjusted,
        "m": int(m),
    }


def run_all_tests(pairs: list[dict[str, Any]], stats_seed: int) -> dict[str, Any]:
    status_table = build_status_contingency_3x3(pairs)
    status_test = run_stuart_maxwell_or_bowker(status_table)
    mcnemar_test = run_mcnemar_test(pairs)
    wilcoxon_test = run_wilcoxon_test(pairs)
    effects = compute_effect_sizes(pairs, wilcoxon_test["p_value"])
    ci = stratified_bootstrap_ci(pairs, seed=stats_seed)
    holm = holm_correction([
        status_test["p_value"],
        mcnemar_test["p_value"],
        wilcoxon_test["p_value"],
    ])

    return {
        "status_3class_test": status_test,
        "should_refuse_mcnemar": mcnemar_test,
        "continuous_wilcoxon": wilcoxon_test,
        "effect_sizes": effects,
        "bootstrap_95_ci": ci,
        "multiple_comparison_holm": holm,
    }
