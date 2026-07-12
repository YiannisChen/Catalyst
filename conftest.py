"""
Repo-root conftest — ensures editable installs resolve under the repo root.

If catalyst_agents.__file__ does not resolve under the repo, the venv's
editable .pth is stale (likely from a deleted worktree).  The error message
includes the fix command.
"""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

_REPO_ROOT = Path(__file__).resolve().parent
_FIX_CMD = (
    ".venv/bin/pip install -e \"packages/data-core[dev]\" "
    "-e packages/agents -e packages/app -e packages/eval"
)


def pytest_configure(config):
    """Verify catalyst_agents resolves inside the repo root."""
    try:
        import catalyst_agents
    except ImportError:
        return  # package not installed — not a path-staleness issue

    resolved = Path(catalyst_agents.__file__).resolve()
    try:
        resolved.relative_to(_REPO_ROOT)
    except ValueError:
        sys.stderr.write(
            f"\nSTALE EDITABLE INSTALL: catalyst_agents resolves to\n"
            f"  {resolved}\n"
            f"which is OUTSIDE the repo root\n"
            f"  {_REPO_ROOT}\n"
            f"Fix: cd {_REPO_ROOT} && {_FIX_CMD}\n\n"
        )
        pytest.exit(
            f"catalyst_agents resolves outside repo root: {resolved}\n"
            f"Reinstall: {_FIX_CMD}",
            returncode=1,
        )
