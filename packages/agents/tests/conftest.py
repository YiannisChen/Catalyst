from __future__ import annotations

import sys
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_DIR.parents[1]
MONOREPO_PACKAGES = [
    REPO_ROOT,
    PACKAGE_DIR,
    PACKAGE_DIR / "tests",
    PACKAGE_DIR.parent / "eval",
    PACKAGE_DIR.parent / "data-core",
]

for package_path in MONOREPO_PACKAGES:
    package_str = str(package_path)
    if package_str not in sys.path:
        sys.path.insert(0, package_str)
