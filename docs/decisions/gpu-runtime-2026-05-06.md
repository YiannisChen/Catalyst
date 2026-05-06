# P1-T01 GPU Runtime Decision (2026-05-06)

## Scope

- Task: `P1-T01` real embedding run for L1 `clean_assets` rows only
- Script: `packages/data-core/scripts/build_embeddings_gpu.py`
- Input DB: `data/catalyst_eval_frozen_v2.db`
- Expected rows: `13474`
- Embedding model: `BAAI/bge-m3`
- Revision pin: `TBD (must be fixed before run)`

## Runtime Selection

- Provider: `RunPod`
- Instance class: `1x RTX 4090` (fallback: `1x RTX A5000` or `1x A10`)
- Region: `Any low-latency region with immediate capacity`
- Operator: `yiannischen`

## Cost and Time Estimate

- Estimated wall time: `20-60 minutes` (depends on model download/cache and batch speed)
- Estimated total cost: `~$0.5 - $2.0`
- Assumption: single run, no reruns, no long idle time

## Security and Access

- HF auth token (if needed) must be injected via runtime env, never committed.
- No plaintext secrets written to repo.
- Output artifacts contain hashes/metadata only.

## Output and Handoff

- VPS output directory: `~/Catalyst/data/embeddings/`
- Required artifacts:
  - `bge_m3_eval_frozen_v2.npy`
  - `asset_id_index.json`
  - `manifest.json`
- Local pullback target:
  - `/Users/yiannischen/Desktop/Catalyst/data/embeddings/`

## Validation Gates (must pass)

1. `manifest.json` records:
   - `n_vectors = 13474`
   - `embedding_dim = 1024`
   - `dtype = float32`
   - `embedding_object_scope = l1_clean_assets_rows`
2. `manifest.json` includes `db_sha256` matching:
   - `14231853db606cf5c422ba333a0c1fdf599ffb3b845f15bd3de9456694b7223f`
3. Output hashes (`numpy_sha256`, `index_sha256`) are present and non-empty.

## Failure / Retry Policy

- If run fails before artifacts are complete: discard partial outputs and rerun once.
- If row count or scope mismatches expected contract: treat as invalid run and rerun.
- If provider instability persists: switch to fallback instance class/provider.

## Notes

- This decision doc satisfies plan OQ#12 runtime-record requirement before real GPU execution.
- T01 is L1-only by design; L2 vectors are produced later in `P1-T03`.
