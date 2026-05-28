# R1 External Baseline Protocol Spec

## Scope

This protocol defines an external consistency check for Catalyst scoring and status behavior using existing artifacts only.

No cloud rerun required.

## Sample Construction

- Use a fixed 20-case sample from existing pulled-back summaries.
- Ensure class mix includes SUFFICIENT/PARTIAL/INSUFFICIENT expectations.
- Use deterministic case list and frozen inputs.

## Evidence Parity Contract

- External baseline must evaluate the same evidence chunks as Catalyst.
- All judgments are based on same evidence chunks and same case metadata.
- Do not introduce additional retrieval sources.

## Metrics

Compute at least:

- Pearson correlation on relevance/confidence score vectors.
- Spearman rank correlation on same vectors.
- Bucket agreement (high/medium/low relevance buckets).

## Acceptance Thresholds

- Pearson >= 0.70
- Spearman >= 0.70
- Bucket agreement >= 0.80

## Fail Conditions

- Any metric below threshold.
- Protocol drift: mismatched chunk set, missing case rows, or non-deterministic sampling.

## Reporting

- Provide per-case discrepancy table.
- Provide aggregate pass/fail summary and remediation notes.
