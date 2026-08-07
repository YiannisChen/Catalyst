"""Git HEAD revision binding for production GPU execution (Finding 3)."""

from __future__ import annotations

import pytest

CODE_REVISION = "04dabde9f44e9638d1b5ebad0eed002f5475c1a3"


def _runner(stdout: str, status: str = "", fail: bool = False):
    def run(*args, cwd=None):
        if fail:
            raise RuntimeError("git command failed")
        if args == ("rev-parse", "HEAD"):
            return stdout
        if args == ("status", "--porcelain"):
            return status
        raise AssertionError(f"unexpected git args: {args}")

    return run


def test_resolver_accepts_matching_head():
    from catalyst_data.retrieval.git_revision import resolve_git_revision

    sha = resolve_git_revision(
        repo_root=".", expected=CODE_REVISION, git_runner=_runner(CODE_REVISION),
    )
    assert sha == CODE_REVISION


def test_resolver_rejects_mismatched_head():
    from catalyst_data.retrieval.git_revision import resolve_git_revision

    with pytest.raises(ValueError, match="does not match HEAD"):
        resolve_git_revision(
            repo_root=".", expected="0" * 40,
            git_runner=_runner(CODE_REVISION),
        )


def test_resolver_rejects_dirty_worktree():
    from catalyst_data.retrieval.git_revision import resolve_git_revision

    with pytest.raises(RuntimeError, match="dirty"):
        resolve_git_revision(
            repo_root=".", expected=CODE_REVISION,
            git_runner=_runner(CODE_REVISION, status=" M packages/agents/x.py\n"),
        )


def test_resolver_rejects_malformed_head_sha():
    from catalyst_data.retrieval.git_revision import resolve_git_revision

    with pytest.raises(ValueError, match="malformed"):
        resolve_git_revision(
            repo_root=".", expected=CODE_REVISION,
            git_runner=_runner("not-a-sha"),
        )


def test_resolver_rejects_malformed_expected_sha():
    from catalyst_data.retrieval.git_revision import resolve_git_revision

    with pytest.raises(ValueError, match="code-revision"):
        resolve_git_revision(
            repo_root=".", expected="short",
            git_runner=_runner(CODE_REVISION),
        )


def test_resolver_skips_clean_check_when_not_required():
    from catalyst_data.retrieval.git_revision import resolve_git_revision

    sha = resolve_git_revision(
        repo_root=".", expected=CODE_REVISION, require_clean=False,
        git_runner=_runner(CODE_REVISION, status=" M dirty"),
    )
    assert sha == CODE_REVISION
