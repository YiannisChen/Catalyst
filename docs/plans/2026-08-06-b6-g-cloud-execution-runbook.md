# B6-G Cloud Execution Runbook (Pre-Cloud Amendment)

Date: 2026-08-06
Branch: `recovery/b2o-data-readiness`

This runbook is the operator playbook for the GPU embedding run and the local
LanceDB import. It binds every command to the six frozen production
identities. Nothing here may be run on this machine unless explicitly
executed by an authorized operator on the GPU host or the local import host.

The execution revision is NOT hardcoded in this file. Both the GPU embedding
run and the local import must use the same manager-approved clean commit,
resolved at run time as `B6_CODE_REVISION` (see section 3). Writing a literal
"final commit SHA" here would be self-referential, because this file's content
participates in the commit identity.

## 1. Frozen production identities

| Identity | Value |
| --- | --- |
| snapshot | `7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49` |
| db_sha | `bb37b213091e256033fa00272cb7a85617dbcddf69d6fe9b515840cd9f1ebe40` |
| corpus | `3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc` |
| source_bundle | `8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2` |
| probe | `25a596e98e4ff5e38f69f64f55404ddb05ab109c2c47d41f615c8744ec19ec23` |
| postbuild | `9f6c62fb0b077b1e946b4c6ee6779a6866b3c835b18acf213a045b75d91c472b` |

Supporting paths (local):

- Active snapshot pointer: `data/manifests/active_data_snapshot.json`
- Production DB: `data/snapshots/catalyst_b2o_7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49.db`
- Source bundle: `data/source_bundles/source_8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2`
- Universe: `37c5da06c2defd8f4d18b68b3499bc4e05ecbb4c97bc0ac3be62a7d7aaea596a`
- Chunk count: `295506`
- SQLite schema version: `13` (integrity `ok`, foreign_key_check `0`)

## 2. Scope and hard stop conditions

Run only the steps below. Never run provider ingestion, B7, or the frontend.

- Do not run any provider, transport, or ingestion entrypoint.
- Do not run B7 evaluation/release or the frontend stack.
- Do not call any model provider API or download weights from this repo
  outside the explicitly authorized bootstrap stage (section 4).
- Do not modify `data/` frozen DB, candidate DB, snapshot, or source bundle.
- Do not stage, commit, push, or open a PR.
- No `--force` and no identity-bypass argument exists in either CLI.

Stop immediately and escalate when any of the following is observed:

1. Any identity mismatch (snapshot, db SHA, corpus, source bundle, probe,
   postbuild, code revision).
2. Git worktree is dirty at execution time or `--code-revision` != HEAD.
3. SQLite `user_version != 13`, `integrity_check != ok`, or any
   `foreign_key_check` violation.
4. Metadata stream count != `295506` or chunk-id order drift.
5. Embedding artifact dtype/dimension/count/model revision mismatch or
   `code_revision` != the import host's `B6_CODE_REVISION`.
6. Target disk free space below the preflight requirement.
7. Any unexpected (non-availability) exception during import; do not retry
   blindly. Preserve logs; the failed non-active staging table, checkpoint,
   and staging_state are removed and the next run starts fresh with plain
   `execute` (never `--resume`).
8. An operator interrupt (KeyboardInterrupt); preserve staging + checkpoint +
   staging_state and resume, never restart from scratch without an explicit
   manager decision.

## 3. Execution revision contract (after manager commit)

Both the GPU host and the local import host MUST be checked out to the same
manager-approved clean commit. On each host, after obtaining that commit:

```bash
cd /path/to/Catalyst
git checkout <manager-approved-commit-sha>
export B6_CODE_REVISION="$(git rev-parse HEAD)"
test -z "$(git status --porcelain)"
test "${#B6_CODE_REVISION}" -eq 40
```

- The same manager-approved commit is used for GPU embedding AND local import.
- Every GPU/import command below passes `--code-revision "$B6_CODE_REVISION"`.
- The embedding artifact's `code_revision` (recorded in `index_manifest.json`)
  must equal the local import host's `B6_CODE_REVISION`; the import CLI fails
  closed if it does not.
- The frozen data identities in section 1 are unchanged by the commit.

## 4. GPU host — environment and model bootstrap (authorized stage)

This section is a two-phase contract:

1. **Bootstrap (explicitly authorized, download allowed once).** Verify the
   host, then fetch the pinned model. This is a separate operator decision
   from production embedding.
2. **Production embedding (offline, no download).** Run after the pinned model
   is verified present and the batch=1 CUDA preflight passes.

Environment checks before bootstrap:

```bash
nvidia-smi                                   # GPU present; record model + CUDA version
python3 --version                            # Python 3.11+ (3.12.7 used locally)
.venv/bin/pip show lancedb | grep -E "^(Name|Version):"   # lancedb==0.30.2
.venv/bin/python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
df -h <artifact_dir>                         # disk capacity for 295506 x 1024 f32 vectors
```

Bootstrap download (authorized only; pinned revision, never floating):

```bash
python - <<'PY'
from huggingface_hub import snapshot_download
snapshot_download(repo_id="BAAI/bge-m3", revision="5617a9f61b028005a4858fdac845db406aefb181")
PY
```

- Only `BAAI/bge-m3` at `revision=5617a9f61b028005a4858fdac845db406aefb181`
  may be fetched. No floating revision, no other embedding model, no CPU
  fallback.
- After the model is verified, production embedding runs with:

```bash
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
```

- Production model resolution is strictly offline: the loader calls
  `snapshot_download(repo_id=BGE_M3_MODEL, revision=<pinned>, local_files_only=True)`.
  A cache miss fails closed with an error stating that downloading is only
  authorized in this bootstrap stage; the production embedding path never
  auto-connects to fetch weights.
- A `batch=1` CUDA preflight must pass before the full corpus starts; the
  production loader refuses to load the model when CUDA is unavailable and
  runs a batch-one contract check (`_load_real_cuda_embedder`).
- Record for the run report: GPU model, CUDA version, model revision, and
  peak allocated/reserved VRAM (`torch.cuda.max_memory_allocated()` /
  `torch.cuda.max_memory_reserved()`).

## 5. GPU host — source bundle preflight and upload

1. Upload the source bundle to the GPU host and verify checksums:

```bash
cd <bundle-upload-dir>/source_8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2
shasum -a 256 -c checksums.sha256
cat source_bundle_manifest.json   # must list chunk_count 295506 and the six identities above
```

2. Confirm the local active DB SHA and source bundle identity have not changed
   before starting any GPU work:

```bash
shasum -a 256 data/snapshots/catalyst_b2o_7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49.db
# expect bb37b213091e256033fa00272cb7a85617dbcddf69d6fe9b515840cd9f1ebe40
```

## 6. Embedding command (GPU host, offline production phase)

Start inside `tmux` so the long run survives a dropped session:

```bash
tmux new -s b6g
cd /path/to/Catalyst
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
.venv/bin/python packages/data-core/scripts/build_corpus_embeddings_gpu.py \
  --source-bundle data/source_bundles/source_8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2 \
  --output-dir <artifact_dir> \
  --batch-size 32 \
  --expected-source-bundle-id 8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2 \
  --expected-snapshot-id 7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49 \
  --expected-corpus-manifest-id 3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc \
  --expected-probe-report-id 25a596e98e4ff5e38f69f64f55404ddb05ab109c2c47d41f615c8744ec19ec23 \
  --expected-postbuild-readiness-id 9f6c62fb0b077b1e946b4c6ee6779a6866b3c835b18acf213a045b75d91c472b \
  --code-revision "$B6_CODE_REVISION"
```

The CLI refuses to run with a dirty worktree or a mismatched HEAD and never
loads the model before CUDA + revision preflight. Batch size may be tuned
(`--batch-size`), but every shard is written to `<artifact_dir>/shards/` with
an atomic checkpoint. A production `status=completed` report is written only
after runtime info is collected and validated: `gpu_name`, `torch_version`,
and `cuda_version` must be non-empty, `peak_memory_allocated >= 0`, and
`peak_memory_reserved >= peak_memory_allocated`. Empty GPU/CUDA/VRAM fields
block the completed report (the run fails instead).

Monitor:

```bash
tmux attach -t b6g
# separate shell:
watch -n 30 'du -sh <artifact_dir>; ls <artifact_dir>/shards | wc -l'
```

Resume after an interrupt (checkpoint-bound, identical identities):

```bash
.venv/bin/python packages/data-core/scripts/build_corpus_embeddings_gpu.py \
  <same arguments> --resume
```

Embedding completion is NOT B6 completion. The embedding run only produces a
`vectors_staged` artifact.

## 7. Artifact return and verification (local)

1. Copy `<artifact_dir>` back to the local import host (rsync/scp) and verify:

```bash
cd <artifact_dir>
shasum -a 256 -c checksums.sha256
cat index_manifest.json   # artifact_state=vectors_staged, model revision pinned, dimension 1024, vector_count 295506
cat gpu_run_report.json   # status=completed, identities, CUDA, peak VRAM, artifact hashes
```

2. Confirm no `non_production` marker exists in `index_manifest.json`.
3. Confirm `code_revision` in `index_manifest.json` equals the import host's
   `B6_CODE_REVISION` (section 3). The import CLI rejects a mismatch.
4. Validate the GPU run report:

```bash
python - <<'PY'
import json
import os
from pathlib import Path
report = json.loads(Path("gpu_run_report.json").read_text())
assert report["status"] == "completed", report["status"]
assert report["schema_version"] == "gpu_run_report_v1"
assert report["source_bundle_id"] == "8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2"
assert report["snapshot_id"] == "7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49"
assert report["corpus_manifest_id"] == "3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc"
assert report["probe_report_id"] == "25a596e98e4ff5e38f69f64f55404ddb05ab109c2c47d41f615c8744ec19ec23"
assert report["postbuild_readiness_id"] == "9f6c62fb0b077b1e946b4c6ee6779a6866b3c835b18acf213a045b75d91c472b"
assert report["code_revision"] == os.environ["B6_CODE_REVISION"]
assert report["model_revision"] == "5617a9f61b028005a4858fdac845db406aefb181"
assert report["dimension"] == 1024 and report["dtype"] == "float32" and report["normalization"] == "l2"
assert report["vector_count"] == 295506
# Production completed reports must never carry empty GPU/CUDA/VRAM fields.
assert isinstance(report["gpu_name"], str) and report["gpu_name"]
assert isinstance(report["torch_version"], str) and report["torch_version"]
assert isinstance(report["cuda_version"], str) and report["cuda_version"]
assert isinstance(report["peak_memory_allocated"], (int, float)) and report["peak_memory_allocated"] >= 0
assert isinstance(report["peak_memory_reserved"], (int, float))
assert report["peak_memory_reserved"] >= report["peak_memory_allocated"]
checksums = {}
for line in Path("checksums.sha256").read_text().splitlines():
    digest, name = line.split(None, 1)
    checksums[name.strip()] = digest
assert report["artifact_hashes"] == checksums, "artifact hashes drift"
print("gpu_run_report OK")
PY
```

## 8. Local import — preflight (zero-write)

```bash
.venv/bin/python packages/data-core/scripts/import_corpus_vectors_lancedb.py preflight \
  --active-snapshot-pointer data/manifests/active_data_snapshot.json \
  --source-bundle data/source_bundles/source_8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2 \
  --embedding-artifact <artifact_dir> \
  --lancedb-dir <lancedb_dir> \
  --active-generation-pointer <lancedb_dir>/active_generation.json \
  --expected-source-bundle-id 8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2 \
  --expected-snapshot-id 7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49 \
  --expected-corpus-manifest-id 3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc \
  --expected-probe-report-id 25a596e98e4ff5e38f69f64f55404ddb05ab109c2c47d41f615c8744ec19ec23 \
  --expected-postbuild-readiness-id 9f6c62fb0b077b1e946b4c6ee6779a6866b3c835b18acf213a045b75d91c472b \
  --expected-db-sha bb37b213091e256033fa00272cb7a85617dbcddf69d6fe9b515840cd9f1ebe40 \
  --code-revision "$B6_CODE_REVISION"
```

Preflight verifies: git clean + HEAD, snapshot pointer path/ID/SHA, SQLite
v13/integrity/FK, current corpus manifest, source bundle identity/checksums/
count, embedding artifact identity/checksums/dtype/dim/count/model revision/
code_revision, metadata stream count (`295506`) and order, target disk
capacity, and staging/active pointer status. It writes nothing.

## 9. Local import — execute / resume

```bash
.venv/bin/python packages/data-core/scripts/import_corpus_vectors_lancedb.py execute \
  <same arguments as preflight>
```

- Creates a unique `chunks__staging__*` table (never overwrites the active
  table) using the production PyArrow schema (`vector` fixed_size_list
  float32 1024, `ticker_associations` list<string>, nullable dedup fields).
- Imports in bounded batches of at most 500.
- `KeyboardInterrupt` preserves the staging table, checkpoint, and
  `staging_state.json`; resume with:

```bash
.venv/bin/python packages/data-core/scripts/import_corpus_vectors_lancedb.py execute --resume \
  <same arguments as preflight>
```

### 9.1 Commit boundary and failure states

The active-generation pointer (`active_generation.json`) is the **final
commit boundary**. The state machine is:

- **PRE_COMMIT:** the pointer still names the old generation (or no
  generation). Any failure may roll back the staging table, checkpoint,
  `staging_state.json`, and the embedding artifact manifest/checksums. The
  old pointer bytes are never touched.
- **COMMITTED:** the pointer has been atomically switched to the new table.
  From this point the new table is protected: no failure may clear, drop, or
  roll back the active table or the pointer. Post-commit cleanup or report
  failures can only downgrade the run to `committed_with_warning`.

Failure semantics are fixed:

- **Controlled interrupt (KeyboardInterrupt):** staging table, checkpoint,
  and `staging_state.json` are preserved; active pointer bytes are unchanged;
  `execute --resume` continues from the exact completed batch offset.
- **Ordinary pre-commit exception:** only the current run's non-active
  staging table is dropped (the active-generation pointer is re-read before
  the drop; a concurrent switch or corrupt pointer fails closed); this run's
  checkpoint and `staging_state.json` are removed; the active table and
  active pointer bytes are unchanged; the embedding artifact is restored to
  its pre-import state. The next run starts fresh with plain `execute`
  (never `--resume`). No other staging table or the active table is ever
  deleted.
- **Post-commit warning (committed_with_warning):** once the pointer is
  committed, a `staging_state` cleanup failure, artifact-payload drift, or
  report-write failure produces `status=committed_with_warning` and exit
  code `2`. The active generation is preserved; nothing is rolled back and
  no table is dropped. Exit code `2` means **the generation is committed but
  a post-commit step failed** — it is not an uncommitted failure. The run
  report (or the structured stderr JSON when the report could not be
  written) records the exact committed identity: `table_name`,
  `index_manifest_id`, `source_bundle_id`, `snapshot_id`,
  `corpus_manifest_id`.
- **Retry after committed_with_warning:** re-running plain `execute` with
  the same arguments is safe. The CLI detects that the exact generation is
  already committed and writes `status=already_committed` without creating a
  second staging table or duplicate import.
- Only after persisted validation passes is `active_generation.json` written
  atomically as the last step before commit; the import report is written
  atomically to `<lancedb_dir>/import_report.json` with no secrets or
  credentials.

### 9.1a Exit codes and the pointer-uncertainty rule

- `exit 0` = `committed` or `already_committed` (a retry detected the exact
  generation already active; no duplicate import was performed).
- `exit 2` = `committed_with_warning`. The generation IS committed; a
  post-commit step (staging_state cleanup, report write, pointer identity
  re-verification) failed. Do NOT blindly re-run `execute`; first read the
  report/stderr JSON and the active pointer. A plain retry is only safe
  because it is detected as `already_committed` (still exit 0) and never
  creates a second staging table.
- `exit 1` = a confirmed ordinary (pre-commit) failure: the active pointer
  was not committed, the failed staging table/checkpoint/staging_state were
  removed, and the next run starts fresh with plain `execute`.
- When the pointer identity is missing, corrupt, or mismatched after a
  commit, the CLI preserves the staging state (`staging_state.json`, never
  deletes recovery state), does not drop/clear any table, does not modify
  the pointer, and returns `committed_with_warning` (exit 2).
- SQLite, source bundle, and artifact payload files (`vectors.npy`,
  `chunk_ids.json`) are never modified by the CLI.

### 9.2 Post-commit verification (operator checks)

```bash
cat <lancedb_dir>/active_generation.json
# Must show: schema_version=active_generation_v1, table_name=<active>,
# index_manifest_id=<64-hex>, source_bundle_id/snapshot_id/corpus_manifest_id
# matching section 1, and chunk_count=295506.
.venv/bin/python - <<'PY'
import json, lancedb
from pathlib import Path
pointer = json.loads(Path("<lancedb_dir>/active_generation.json").read_text())
assert pointer["schema_version"] == "active_generation_v1"
assert pointer["chunk_count"] == 295506
table = lancedb.connect("<lancedb_dir>").open_table(pointer["table_name"])
assert table.count_rows() == pointer["chunk_count"] == 295506
print("active table:", pointer["table_name"], "rows:", table.count_rows())
PY
cat <lancedb_dir>/import_report.json
# status must be committed or committed_with_warning (never ok=false after commit);
# table_name must equal the active pointer's table_name.
python3 - <<'PY'
import json
from pathlib import Path
report = json.loads(Path("<lancedb_dir>/import_report.json").read_text())
pointer = json.loads(Path("<lancedb_dir>/active_generation.json").read_text())
assert report["status"] in ("committed", "committed_with_warning")
assert report["table_name"] == pointer["table_name"]
assert report["index_manifest_id"] == pointer["index_manifest_id"]
print("report identity OK:", report["status"])
PY
```

If `import_report.json` is missing but the pointer names a table with
`295506` rows, the run was `committed_with_warning` (report write failed);
re-run plain `execute` with identical arguments to recover the report
idempotently (`already_committed`).

## 10. What embedding completion means (and does not mean)

Embedding completion produces a `vectors_staged` artifact only; it is never
equal to B6 completion. B6 completion additionally requires:

1. LanceDB import of the 295506 vectors through the operator CLI
   (preflight → execute → persisted validation → active pointer).
2. Four-arm artifacts (fts5, dense, hybrid, reranked) produced by the
   runtime against the imported dense index.
3. Union judgment pool generation from the four-arm artifacts.
4. Human judgments over the union pool.
5. The B6 experiment/analysis and its report.

Union pool generation and its tests use an **independent literal oracle**:
the expected union order (`a,b,c,d,e,f` for the fixture arms), per-arm
chunk-id tuples, manifest identities, and the source artifact id are
hardcoded literals. No test derives the expected union from
`per_arm_chunk_ids` or from a production helper, and the fixture no longer
contains a circular "populated by the production contract" oracle. The pool
rejects a missing arm, a duplicate chunk id within one arm, a malformed
chunk id, and a source-artifact identity mismatch.

Until those five items are finished and verified, the status is not
`READY_FOR_FINAL_MANAGER_REVIEW` on B6 completeness grounds. The manager
review gate for GPU execution is decided from the evidence report, not from
the embedding run alone.

## 11. Final local verification checklist

- [ ] Same manager-approved clean commit on GPU and import hosts;
      `B6_CODE_REVISION` length 40 and artifact `code_revision` matches it.
- [ ] Active DB SHA still `bb37b213091e256033fa00272cb7a85617dbcddf69d6fe9b515840cd9f1ebe40`
- [ ] SQLite `PRAGMA user_version = 13`, `integrity_check = ok`,
      `foreign_key_check = 0`, current corpus =
      `3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc`
- [ ] Source bundle checksum file verifies and `chunk_count = 295506`
- [ ] Import report `ok=true`, table count `295506`, index_manifest_id bound
- [ ] Dense filtered query smoke test returns results under the active pointer
- [ ] `gpu_run_report.json` status=completed and identities match section 1
      (source_bundle/snapshot/corpus/probe/postbuild + code_revision)
- [ ] `gpu_run_report.json` records GPU name, CUDA version, peak
      allocated/reserved VRAM, and artifact hashes equal to checksums.sha256
- [ ] No provider/B7/frontend run, no model download outside bootstrap,
      no GPU use on this host, no stage/commit/push/PR
