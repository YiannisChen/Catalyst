"""Git HEAD revision resolution for production GPU execution (Finding 3).

Production execution binds ``code_revision`` to the real ``git rev-parse HEAD``
and requires a clean worktree. The resolver accepts an injected git runner so
mock/test mode never depends on the surrounding checkout state.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

_HEX40 = re.compile(r"[0-9a-f]{40}\Z")


def _subprocess_git_runner(*args: str, cwd: str) -> str:
    import subprocess

    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def resolve_git_revision(
    *,
    repo_root: str | Path,
    expected: str | None = None,
    require_clean: bool = True,
    git_runner: Callable[..., str] | None = None,
) -> str:
    """Resolve HEAD; fail closed on dirty worktree or SHA mismatch.

    Args:
        repo_root: Git repository root for the injected runner.
        expected: Optional caller-supplied revision. When provided it must
            be an exact 40-character SHA equal to HEAD.
        require_clean: When True, ``git status --porcelain`` must be empty.
        git_runner: Injectable runner for tests (``*args, cwd=...`` -> stdout).
    """
    runner = git_runner or _subprocess_git_runner
    sha = runner("rev-parse", "HEAD", cwd=str(repo_root)).strip()
    if _HEX40.fullmatch(sha) is None:
        raise ValueError(f"malformed Git HEAD revision: {sha!r}")
    if require_clean:
        status = runner("status", "--porcelain", cwd=str(repo_root)).strip()
        if status:
            raise RuntimeError(
                "Git worktree is dirty; production execution requires a clean worktree"
            )
    if expected is not None:
        if not _HEX40.fullmatch(expected):
            raise ValueError("--code-revision must be a 40-character Git SHA")
        if expected != sha:
            raise ValueError(f"--code-revision {expected} does not match HEAD {sha}")
    return sha


__all__ = ["resolve_git_revision"]
