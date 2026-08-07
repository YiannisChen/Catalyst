# Protected Artifact CI Contract

Date: 2026-08-06

## Purpose

Sixteen data-core tests (`test_s3_corpus_items.py`,
`test_s3_frozen_db_readonly.py`, `test_s3_timestamp_canonical.py`,
`test_s3_watermark.py`) read frozen/protected SQLite artifacts that are NOT
committed to the repository:

- `data/catalyst_dev_ws4b.db`
- `data/catalyst_eval_frozen_v2.db`

These tests are marked with the `protected_artifact` pytest marker. The repo
never fakes these databases, never re-pins their SHAs, and never blanket-skips
the tests: every skip names the exact missing artifact path.

## Marker semantics

`@pytest.mark.protected_artifact("<repo-root-relative-path>")`

- When the referenced artifact file exists → the test runs normally.
- When the artifact file is missing and `CATALYST_REQUIRE_PROTECTED_DB` is
  unset → the test is SKIPPED with a reason that includes the missing path.
- When the artifact file is missing and `CATALYST_REQUIRE_PROTECTED_DB=1` →
  the test FAILS (hard error) instead of skipping.

Implementation: `packages/data-core/tests/conftest.py`
(`pytest_configure` + `pytest_runtest_setup` + `_protected_artifact_missing`).

## Default open-source CI (no artifacts)

Default CI runs without the protected DBs. The 16 marked tests are skipped
with explicit missing-path reasons; all other tests run normally. To keep the
signal visible, CI output should show the skip counts:

```bash
.venv/bin/python -m pytest packages/data-core/tests -q -rs
```

Expected default outcome (without protected artifacts):

- protected tests: skipped (16) with missing-path reasons
- no protected-DB test is silently "passing" or blanket-ignored

## Private artifact CI (with frozen DBs)

A private/internal CI job that has access to the frozen artifacts runs with
the environment variable set so a missing artifact is a hard failure rather
than a skip:

```bash
CATALYST_REQUIRE_PROTECTED_DB=1 .venv/bin/python -m pytest packages/data-core/tests -q
```

In this mode a missing `data/catalyst_dev_ws4b.db` or
`data/catalyst_eval_frozen_v2.db` fails the run, which prevents the open-source
skip path from masking an artifact regression in the private pipeline.

## Governance

- Do not delete or rename the protected artifact paths without updating the
  marker arguments.
- Do not replace the marker with `pytest.mark.skip` or `pytest.mark.skipif`
  (that would hide the missing-path reason and break strict mode).
- Do not commit copies of the protected DBs or re-pin their SHAs.
- The 16 protected tests remain the single source of truth for frozen-DB
  identity/byte checks; marker + strict mode only control skip-vs-fail.
