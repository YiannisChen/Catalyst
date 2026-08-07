"""Static regression tests for the B6-G cloud execution runbook (Tasks 1-2).

The runbook must never hardcode the pre-commit HEAD as the final execution
revision; production ``resolve_git_revision()`` rejects a revision that does
not equal the clean-checkout HEAD. Both GPU embedding and local import must
bind to the same manager-approved clean commit via ``B6_CODE_REVISION``.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNBOOK = REPO_ROOT / "docs" / "plans" / "2026-08-06-b6-g-cloud-execution-runbook.md"

OLD_EXECUTION_REVISION = "04dabde9f44e9638d1b5ebad0eed002f5475c1a3"
PINNED_MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"


def _text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def test_runbook_does_not_hardcode_old_execution_revision():
    """The pre-commit HEAD must not appear anywhere as the execution revision."""
    assert OLD_EXECUTION_REVISION not in _text()


def test_gpu_and_import_commands_use_b6_code_revision_variable():
    text = _text()
    # GPU embedding command + import preflight + import execute all use the var.
    assert text.count('--code-revision "$B6_CODE_REVISION"') >= 2


def test_runbook_defines_revision_after_clean_checkout():
    text = _text()
    assert 'B6_CODE_REVISION="$(git rev-parse HEAD)"' in text
    assert 'test -z "$(git status --porcelain)"' in text
    assert 'test "${#B6_CODE_REVISION}" -eq 40' in text


def test_runbook_requires_same_commit_on_gpu_and_import_hosts():
    text = _text()
    assert "same manager-approved" in text.lower()


def test_runbook_requires_artifact_code_revision_equals_import_head():
    text = _text()
    assert "code_revision" in text
    assert "import checkout HEAD" in text or "B6_CODE_REVISION" in text


def test_runbook_pins_bge_m3_revision_and_offline_environment():
    text = _text()
    assert PINNED_MODEL_REVISION in text
    assert "HF_HUB_OFFLINE=1" in text
    assert "TRANSFORMERS_OFFLINE=1" in text


def test_runbook_requires_batch_one_cuda_preflight_and_vram_record():
    text = _text()
    assert "batch=1" in text
    assert "preflight" in text
    assert "peak" in text
    assert "allocated" in text
    assert "reserved" in text


def test_runbook_splits_bootstrap_download_from_production_embedding():
    text = _text()
    assert "bootstrap" in text.lower()
    assert "two" in text.lower() or "2." in text


def test_runbook_exports_b6_code_revision():
    assert 'export B6_CODE_REVISION="$(git rev-parse HEAD)"' in _text()


def test_runbook_report_validation_uses_os_environ_and_no_placeholder():
    text = _text()
    assert "import os" in text
    assert 'os.environ["B6_CODE_REVISION"]' in text
    assert "<import-host-B6_CODE_REVISION>" not in text


def test_runbook_documents_exit_code_contract():
    text = _text()
    assert "exit 0" in text
    assert "already_committed" in text
    assert "exit 2" in text
    assert "committed_with_warning" in text
    assert "exit 1" in text
    assert "staging state" in text


def test_runbook_production_loader_is_offline_local_files_only():
    assert "local_files_only=True" in _text()
