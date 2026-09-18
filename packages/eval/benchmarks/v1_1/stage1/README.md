# Catalyst V1.1 — Stage-1 benchmark

**Status: PUBLISHED.** Human Gate 1 authorized publication on 2026-09-15.
The canonical, write-once benchmark consists of this README and the three
files below. Publication used `write_benchmark_dataset_files`; the historical
Q-011 staging names were not copied as public filenames.

## Purpose

The V1.1 Stage-1 benchmark measures the *single-graph* V1.1 runtime
(`run_v1_graph` through the M6 app/SSE boundary) on a frozen, human-adjudicated
12-case set: whether the runtime attributes a signed trading session to the
correct catalyst, refuses when the local/public evidence base does not support
attribution, and reports honest gaps. It is a controlled evaluation artifact,
not a quality claim about any model.

## File layout

| File | Contents |
| --- | --- |
| `cases.jsonl` | The ordered 12 `BenchmarkCase` rows (JSONL, one object per line). |
| `stratification.json` | The declared aggregate strata and per-case coverage. |
| `manifest.json` | `BenchmarkDatasetManifest`: schema, dataset identity, ordered case ids, list/content hashes, case count, reviewer ids, approval authority. |

Published SHA-256 digests:

- `cases.jsonl`: `39b2fc0e5bcabf50e59cac0a86a3113f88e3e21db16acb032bc3505666d0d836`
- `stratification.json`: `85dc3ec393531abb0bbdbccaee5f7238bfe9ac04c7e3f8a11146f76c6ce63473`
- `manifest.json`: `87cc5f3e50c657da6727c42ac1d71b1ba3bc0e430f09534f9d52a9d96d4e7806`

## Contracts

- `BenchmarkCase` — the public V1.1 name for the frozen human-truth case
  contract. `GoldenCase` is retained only as the sealed compatibility alias.
- `BenchmarkDatasetManifest` — the typed dataset-manifest contract.
- `HumanOutputAudit` — the public V1.1 name for the sealed human output-audit
  contract. `Stage1OutputAudit` is retained only for sealed compatibility.
- Loader: `catalyst_eval.v1_1.loader.load_benchmark_cases`.

New V1.1 surfaces use `benchmark` terminology. Sealed historical V1/V1.2
`golden_set` artifacts are compatibility inputs only; they remain readable
through legacy names and are not the public V1.1 benchmark contract. They are
never renamed or rewritten.

## Comparability

The V1.1 Stage-1 report is marked **NON-COMPARABLE** (`Q-002` promoted
environment tuple unrecovered). A Stage-1 score must never be compared with a
legacy V1/V1.2 or baseline number.

## Publication boundary

The published benchmark records **no provider-call ceiling and no USD cost
ceiling**. Approved ceilings are operator decisions made at Human Gate 2 and
are recorded only in the runtime `prepare_summary.json` under
`data/eval_reports/`; they are never part of this publication. Likewise the
publication contains no credentials, secrets, budget values, audit decisions,
or model/human approvals beyond the sealed human adjudication metadata.

Flow around publication:

1. Before Human Gate 2, the operator performs readiness/identity/index checks
   and *calculates a proposed* provider-call and USD budget. No authoritative
   `prepare` runs yet.
2. Human Gate 2 approves the exact ceilings and configures the secret locally.
3. Only then does the operator run the authoritative `prepare` with the
   approved ceilings, followed by `execute`/`audit`/`report`.

## Reporting

Reports are write-once and named `v1_1_stage1_<sha8>.*`, where `<sha8>` is the
first eight hex characters of the clean execution HEAD before any provider
call. Markdown is a deterministic rendering of the sealed JSON and never
supplies a new fact.
