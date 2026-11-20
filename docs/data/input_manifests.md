# Input Manifests for Research, Backfills, and Shadow Comparisons

## Manifest format

```json
{
  "schema_version": 1,
  "invocation": ["-m", "catalyst_data.ingestion.collectors.alpaca", "news-backfill", "--db", "/opt/catalyst/sandbox/news.sqlite3", "--dates", "/opt/catalyst/sandbox/dates.txt"],
  "files": [
    {"path": "/absolute/path/to/input", "sha256": "operator-verified-64-character-lowercase-sha256"}
  ],
  "environment": {
    "PYTHONPATH": "/opt/catalyst/collectors/packages/data",
    "CATALYST_ARCHIVE_DIR": null,
    "CATALYST_DATA_DIR": null,
    "CATALYST_RATELIMIT_DIR": null,
    "CATALYST_RESILIENCE_DB": null,
    "APCA_DATA_URL": null
  }
}
```

This is a structural example. The placeholder hash cannot be used to run a job. `null` means the variable must be unset; an empty string is different from an unset variable. The non-secret Alpaca endpoint `APCA_DATA_URL` must also be checked. Do not include a username, password, or query credentials in the endpoint. Manifests, results, and logs contain neither keys nor key hashes.

- Paths must be absolute. SHA-256 is checked byte-for-byte. A directory hash is calculated from a list of `[relative_path, file_sha256]` pairs sorted by relative path, serialized as ASCII JSON with compact separators. Do not use a regular file hash as a directory hash.
- Research inputs use immutable copies. Create a consistent SQLite copy with the backup API or `.backup`, then hash it. Do not copy a live database file and treat it as a snapshot; a non-empty WAL is rejected. Adding or removing files in a directory changes its hash.
- The manifest covers databases, snapshots, samples, company and peer mappings, correction tables, entry-point code, and input-validation code that are actually read. Research entry points must list their actual default paths as required inputs. The job is rejected if a required input is missing, even when the command is otherwise unchanged.
- `environment` allows only safe settings such as paths and modes; credential variables cannot contain values. It does not claim to record the entire process environment. API keys are injected through the existing secret configuration and do not identify a result.
- Do not list locked evaluation directories as inputs to data jobs. Existing research code is not authorized to read outputs from either evaluation round or use test-set decisions.

## Starting jobs and results

Set `CATALYST_INPUT_MANIFEST` explicitly to select the manifest and `CATALYST_INPUT_RECEIPT` to select a new receipt path. The receipt must be a separate output that does not yet exist. It must not overwrite the manifest or any input. Use a different job ID and receipt filename for each attempt.

At startup, the command, file hashes, and effective environment are validated. On failure, the process exits without opening the target database or starting collection. On success:

1. The first line of standard output is `{"input_manifest": {...}}`, containing the paths and hashes checked, the environment, and the manifest SHA-256.
2. The same receipt is saved as a separate JSON file. Keep it with the result. Existing result file formats are unchanged; the receipt is associated through the job ID.
3. Only then is a result generated or a backfill run. Startup validation does not replace file isolation: no other job may modify an input copy during execution.

Wait across jobs with `catalyst-job NAME ...` and `catalyst-job --wait NAME`; do not use temporary systemd units or pgrep to determine completion. Network batch jobs run only on the US server and during the allowed window, with `RuntimeMaxSec` set.

## Branches and coverage boundaries

- `data/input-preflight`: startup validation for nine `scripts/research/v3/` entry points. Historical fetch-failure statistics now read pre-filtered, hashed status/error-log copies; do not directly cat or tail evaluation logs.
- `data/collectors`: manual Alpaca news backfills, research minute bars, and FINRA and regulatory backfills with explicit dates or settlement dates. Scheduled production collection entry points retain their existing invocation form.
- `data/time-fix`: input validation for manual `daily run/retry/deferred`; `daily scheduled` is unchanged.
- `shadow_compare.py`: the new acceptance entry point uses the same validator in `data/collectors`. It has been verified locally but is not deployed. Its supported scope, missing run records, and cutover blockers are described in `shadow_acceptance.md`; the old server entry point is not protected by this validation.
- For code described here, report its commit and deployment status separately. Local tests do not mean it is enabled on the server. Frozen runners, other legacy research directories, and arbitrary Python function calls are outside this entry-point coverage and must not be used to bypass this rule when starting new jobs.

## Keep fixed-input replay separate from live network runs

Fixed-input replay locks all responses, databases, and configuration to attribute behavior differences. A live-network sandbox also records collection start and end, server response identity, and the input receipt above. It proves only that the provider is currently available; identical manifests do not mean live network results will be identical. Reports for these two run types, for both the old and new daily versions, are reviewed separately.
