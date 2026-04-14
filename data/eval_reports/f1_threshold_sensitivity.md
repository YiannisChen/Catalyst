# F1 Threshold Sensitivity Analysis

Golden set: `v1_2.jsonl` (50 events)
Total causes: 114

## Intra-event Jaccard Distribution
Pairs of distinct causes **within the same event** (should NOT match).
- Count: 78
- Min: 0.0000  Median: 0.0811  P95: 0.2000  Max: 0.2500

## Cross-event / Same-category Jaccard Distribution
Pairs of causes from **different events** that share a category.
- Count: 1287
- Min: 0.0000  Median: 0.0488  P95: 0.1351  Max: 0.3333

## Threshold Sweep

| Threshold | Intra FP | Intra FP % | Cross-cat matches | Cross-cat % |
|-----------|----------|------------|-------------------|-------------|
| 0.05 | 59 | 75.6% | 599 | 46.5% |
| 0.10 | 31 | 39.7% | 158 | 12.3% |
| 0.15 | 12 | 15.4% | 44 | 3.4% |
| 0.20 | 3 | 3.8% | 10 | 0.8% |
| 0.25 | 0 | 0.0% | 2 | 0.2% |
| 0.30 | 0 | 0.0% | 2 | 0.2% |
| 0.35 | 0 | 0.0% | 0 | 0.0% |
| 0.40 | 0 | 0.0% | 0 | 0.0% |
| 0.45 | 0 | 0.0% | 0 | 0.0% |
| 0.50 | 0 | 0.0% | 0 | 0.0% |

## Recommendation
- **Max intra-event Jaccard:** 0.2500
- **Empirically safe threshold:** 0.26

**Raise** threshold from 0.20 to **0.26** to prevent intra-event false positives.

> Jaccard is a transitional matcher. Production should use embedding-based similarity (cosine on sentence-transformer vectors).