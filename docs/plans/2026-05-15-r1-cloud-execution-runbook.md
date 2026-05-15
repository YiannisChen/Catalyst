# R1 Cloud Execution Runbook

## Scope

This runbook defines cloud-only execution steps for R1 validation experiments.

Do NOT run experiments on local machine.

## Cloud Preflight Checks

1. Confirm branch and commit parity:
   - `git fetch origin`
   - `git switch exp/p1-1-cloud-run-20260511`
   - `git reset --hard origin/exp/p1-1-cloud-run-20260511`
2. Confirm Python env and credentials:
   - `PY=/usr/local/miniconda3/envs/py312/bin/python`
   - `source /root/.bashrc`
   - verify API key env is present
3. Confirm input artifacts exist:
   - `r1_input_chunks.jsonl`
   - `v1_2_p1_set.jsonl`

## Experiment 1: External Baseline Grading

Purpose: run external baseline grading on fixed 20-case input chunks.

Local preflight (mock only): `run_r1_external_baseline.py local/mock only`

```bash
$PY scripts/reports/run_r1_external_baseline.py \
  --input /Users/yiannischen/Desktop/Catalyst/data/eval_reports/r1_external/r1_input_chunks.jsonl \
  --output /Users/yiannischen/Desktop/Catalyst/data/eval_reports/r1_external/r1_sonnet_grades.mock.local.jsonl \
  --model claude-sonnet-4-20250514 \
  --mock --limit 3
```

Cloud real run (requires API key in env):

```bash
export AIHUBMIX_API_KEY=...
$PY scripts/reports/run_r1_external_baseline.py \
  --input /root/Catalyst/data/eval_reports/r1_external/r1_input_chunks.jsonl \
  --output /root/Catalyst/data/eval_reports/r1_external/r1_sonnet_grades.jsonl \
  --model claude-sonnet-4-20250514 \
  --base-url https://aihubmix.com/v1 \
  --real
```

## Experiment 2: Coverage-Gap Label Verify (12-case)

Purpose: verify coverage-gap relabel behavior on designated insufficient/refusal set.

Template command:

```bash
RUN_TAG=r1_gap_verify_$(date +%Y%m%d_%H%M%S)
$PY scripts/p1_ablation.py \
  --golden-set /root/Catalyst/packages/eval/golden_set/v1_2_p1_set.jsonl \
  --case-ids g015,g046,g049 \
  --profiles full,no_rerank,no_vector,degraded \
  --continue-on-error \
  --tag ${RUN_TAG} \
  --out-dir /root/Catalyst/data/eval_reports
```

Acceptance: 12/12 expected outputs are INSUFFICIENT across the 3 cases x 4 profiles, verified from ablation `summary` and `per_case` fields.

## Experiment 3: K-Sensitivity Consistency Check

Purpose: verify K sensitivity conclusions remain stable in cloud environment.

Template command:

```bash
$PY scripts/reports/k_sensitivity_dryrun.py \
  --summaries-dir /root/Catalyst/data/eval_reports/p1_runs/p1_full_calibrated_20260514_155759/summaries \
  --retry-summaries-dir /root/Catalyst/data/eval_reports/p1_runs/p1_full_calibrated_retry_g005_20260514_181258/summaries \
  --k-values 1,2,3,4 \
  --output /root/Catalyst/docs/reports/2026-05-15-k-sensitivity-curve.md
```

Acceptance: K sensitivity report includes local-optimum discussion with K=2 comparison line.

## Pullback Templates

After each cloud experiment, pull artifacts back to local workspace.

```bash
# from local machine
scp -P 23 root@117.50.223.51:/root/Catalyst/data/eval_reports/r1_external/r1_sonnet_grades.jsonl \
  /Users/yiannischen/Desktop/Catalyst/data/eval_reports/r1_external/

scp -P 23 root@117.50.223.51:/root/Catalyst/docs/reports/2026-05-15-k-sensitivity-curve.md \
  /Users/yiannischen/Desktop/Catalyst/docs/reports/
```

Then compute local correlation:

```bash
/Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python \
  /Users/yiannischen/Desktop/Catalyst/scripts/reports/compute_r1_correlation.py \
  --catalyst-input /Users/yiannischen/Desktop/Catalyst/data/eval_reports/r1_external/r1_input_chunks.jsonl \
  --external-input /Users/yiannischen/Desktop/Catalyst/data/eval_reports/r1_external/r1_sonnet_grades.jsonl \
  --output-report /Users/yiannischen/Desktop/Catalyst/docs/reports/2026-05-16-r1-external-baseline-results.md
```

## Acceptance Thresholds

- Pearson >= 0.65
- Spearman >= 0.65
- Bucket agreement >= 0.75
- Coverage-gap verify run: 12/12 INSUFFICIENT expected alignment
- K sensitivity: K=2 local-optimum check documented

## Stop Conditions

Stop and notify reviewer if any of the following occurs:
- Missing cloud artifacts
- Metric threshold miss
- Non-deterministic input mismatch
- Any local execution attempt for cloud-only experiments
