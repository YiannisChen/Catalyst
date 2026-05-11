# g013 Dual-Track Model Comparison Design

## Goal

Validate Catalyst value against direct-LLM answers on the same g013 question by comparing reliability, grounding, latency, and cost.

## Step 1: Run Catalyst track (per model)

Use `p1_trace_report.py` for each model under the same tag.

```bash
cd /Users/yiannischen/Desktop/Catalyst

python scripts/p1_trace_report.py \
  --case-id g013 \
  --provider aihubmix \
  --model gemini-2.5-flash-nothink \
  --base-url https://aihubmix.com/v1 \
  --query-override "Why did NVDA move on 2025-10-28? Provide 3 grounded causes with evidence IDs." \
  --out-dir data/eval_reports \
  --tag g013_dual_track

python scripts/p1_trace_report.py --case-id g013 --provider aihubmix --model deepseek-v4-flash --base-url https://aihubmix.com/v1 --query-override "Why did NVDA move on 2025-10-28? Provide 3 grounded causes with evidence IDs." --out-dir data/eval_reports --tag g013_dual_track
python scripts/p1_trace_report.py --case-id g013 --provider aihubmix --model qwen3.6-flash --base-url https://aihubmix.com/v1 --query-override "Why did NVDA move on 2025-10-28? Provide 3 grounded causes with evidence IDs." --out-dir data/eval_reports --tag g013_dual_track
python scripts/p1_trace_report.py --case-id g013 --provider aihubmix --model qwen-turbo --base-url https://aihubmix.com/v1 --query-override "Why did NVDA move on 2025-10-28? Provide 3 grounded causes with evidence IDs." --out-dir data/eval_reports --tag g013_dual_track
python scripts/p1_trace_report.py --case-id g013 --provider aihubmix --model DeepSeek-V3.1-Terminus --base-url https://aihubmix.com/v1 --query-override "Why did NVDA move on 2025-10-28? Provide 3 grounded causes with evidence IDs." --out-dir data/eval_reports --tag g013_dual_track
python scripts/p1_trace_report.py --case-id g013 --provider aihubmix --model claude-opus-4-6 --base-url https://aihubmix.com/v1 --query-override "Why did NVDA move on 2025-10-28? Provide 3 grounded causes with evidence IDs." --out-dir data/eval_reports --tag g013_dual_track
```

Expected outputs: `data/eval_reports/*_g013_p1_trace.summary.json`

## Step 2: Run direct baseline track (same query)

```bash
cd /Users/yiannischen/Desktop/Catalyst

python scripts/g013_direct_baseline.py \
  --provider aihubmix \
  --base-url https://aihubmix.com/v1 \
  --models gemini-2.5-flash-nothink,deepseek-v4-flash,qwen3.6-flash,qwen-turbo,DeepSeek-V3.1-Terminus,claude-opus-4-6 \
  --query "Why did NVDA move on 2025-10-28? Provide 3 grounded causes with evidence IDs." \
  --continue-on-error \
  --out-dir data/eval_reports \
  --tag g013_dual_track
```

Expected output: `data/eval_reports/g013_dual_track_g013_direct_baseline.json`

## Step 3: Generate compare report

```bash
cd /Users/yiannischen/Desktop/Catalyst

python scripts/g013_compare_report.py \
  --direct-json data/eval_reports/g013_dual_track_g013_direct_baseline.json \
  --catalyst-input data/eval_reports \
  --tag g013_dual_track \
  --out-dir data/eval_reports
```

Expected outputs:
- `data/eval_reports/g013_dual_track_compare.json`
- `data/eval_reports/g013_dual_track_compare.md`

## Notes

- Do not modify `packages/eval/golden_set/v1_2_p1_set.jsonl`.
- If some models fail, keep `--continue-on-error` in direct track so comparison can still be generated for available models.
- Comparison script handles missing direct/catalyst side gracefully and marks missing rows in output.
