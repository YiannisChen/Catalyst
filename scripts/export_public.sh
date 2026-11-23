#!/usr/bin/env bash
set -euo pipefail

if (($# != 1)); then
  echo "usage: $0 TARGET_DIRECTORY" >&2
  exit 2
fi
target=$1
mkdir -p "$target"
if [[ -n "$(find "$target" -mindepth 1 -print -quit)" ]]; then
  echo "refusing non-empty export target: $target" >&2
  exit 2
fi
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

# Public source allowlist. Directories are copied recursively, then filtered below.
ALLOWLIST=(
  README.md LICENSE CONTRIBUTING.md SECURITY.md .env.example Makefile .gitignore .gitattributes pyproject.toml requirements-dev.txt
  .github
  apps/web
  packages/api packages/agents packages/data
  contracts
  deploy
  docs/architecture.md docs/project-retrospective.md docs/data/data-card.md docs/data/contract.md docs/data/input_manifests.md docs/data/schedule.md docs/data/legacy_cohort_manifest.schema.json
  docs/system-card.md docs/diagrams docs/demo/screenshots docs/demo/tools docs/demo/promo docs/demo/video
  data/manifests/peer_map_v1.json data/manifests/sources_v1.json data/manifests/source_categories_v1.json data/manifests/heldout_universe_v1.json data/manifests/featured_cases_v1.json data/manifests/featured_cases_v1.sqlite3.sha256
  data/corrections/README.md data/corrections/README_sec.md data/corrections/sec_acceptance_v1.csv data/corrections/time_corrections_v1.csv
  scripts/check.sh scripts/check_ruff.py scripts/ruff_baseline.json scripts/export_api_contract.py scripts/sign_invite_token.py scripts/export_public.sh scripts/fixtures scripts/featured/build_bundle.py scripts/featured/list_candidates.py
)

for path in "${ALLOWLIST[@]}"; do
  [[ -e "$path" ]] || { echo "allowlisted path is missing: $path" >&2; exit 1; }
done

# Copy only public source files. Runtime databases and generated/local paths are
# filtered here so a test or build run cannot poison the next export.
python3 - "$ROOT" "$target" "${ALLOWLIST[@]}" <<'PY'
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

root, target = Path(sys.argv[1]), Path(sys.argv[2])
excluded_dirs = {
    ".local", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules",
    "dist", ".mypy_cache", ".tox", ".git", "boards", "design-reference",
}

def allowed(rel: Path) -> bool:
    parts = rel.parts
    if any(part in excluded_dirs for part in parts) or rel.name == ".DS_Store":
        return False
    if rel.suffix == ".pyc":
        return False
    # Host-specific first-deploy runbook (ports of other services on the server); kept private.
    if rel.as_posix() == "deploy/DEPLOY_STEPS.md":
        return False
    if rel.suffix.lower() in {".db", ".sqlite3"}:
        return len(parts) >= 5 and parts[0] == "packages" and parts[2:4] == ("tests", "fixtures")
    if rel.name == ".env" or (rel.name.startswith(".env.") and rel.as_posix() != ".env.example"):
        return False
    return True

def copy_path(rel: Path) -> None:
    src, dst = root / rel, target / rel
    if src.is_dir() and not src.is_symlink():
        dst.mkdir(parents=True, exist_ok=True)
        for child in sorted(src.iterdir()):
            child_rel = rel / child.name
            if allowed(child_rel):
                copy_path(child_rel)
    else:
        if not allowed(rel):
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            dst.symlink_to(os.readlink(src))
        else:
            shutil.copy2(src, dst)

for item in sys.argv[3:]:
    copy_path(Path(item))
PY

# Remove local/build caches, private design references, research artifacts, and retired collector deployment files.
find "$target" -type d \( -name .git -o -name .local -o -name .pytest_cache -o -name .ruff_cache -o -name __pycache__ -o -name node_modules -o -name dist -o -name .mypy_cache -o -name .tox -o -name boards -o -name design-reference \) -prune -exec rm -rf {} +
rm -rf "$target/apps/web/design"
find "$target" -type f \( -name '.env' -o -name '.env.*' -o -name '*.pyc' -o -name '*.pyo' -o -name '*.mp4' -o -name '.DS_Store' \) ! -path "$target/.env.example" -delete
find "$target" -type f -name '.env.example' ! -path "$target/.env.example" -delete

python3 - "$target" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
errors: list[str] = []
ip_re = re.compile(rb"(?:198\.23\.246\.147|103\.30\.76\.109|117\.50\.245\.104|43\.134\.86\.76)")
key_re = re.compile(rb"(?:sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|xoxb-[A-Za-z0-9-]{20,})")
email_re = re.compile(rb"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
allowed_email = re.compile(rb"(?:[A-Za-z0-9._%+-]+@(?:example\.com|example\.org|example\.test|example\.invalid|test\.com|test\.org)|(?:[0-9]+\+)?YiannisChen@users\.noreply\.github\.com)\Z", re.I)
total = 0
files = 0

for path in sorted(p for p in root.rglob("*") if p.is_file()):
    rel = path.relative_to(root).as_posix()
    size = path.stat().st_size
    files += 1
    total += size
    if size > 10 * 1024 * 1024:
        errors.append(f"file over 10 MB: {rel} ({size} bytes)")
    lower = rel.lower()
    if Path(rel).name.startswith(".env") and rel != ".env.example":
        errors.append(f"environment file outside approved template: {rel}")
    if lower.endswith((".sqlite3", ".db", ".npy")) and not re.search(r"(?:^|/)packages/[^/]+/tests/fixtures/", lower):
        errors.append(f"database or array artifact outside package test fixtures: {rel}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        errors.append(f"could not read {rel}: {exc}")
        continue
    if ip_re.search(data):
        errors.append(f"listed server IP found: {rel}")
    if re.search(rb"/Users/[a-z][a-z0-9_-]+/", data) and not rel.endswith(".lock"):
        errors.append(f"local home path found: {rel}")
    # Tests and fixture payloads intentionally contain redaction examples and synthetic tokens.
    is_test_or_fixture = bool(re.search(r"(?:^|/)(?:tests|fixtures)(?:/|$)", lower))
    if not is_test_or_fixture and key_re.search(data):
        errors.append(f"key-shaped string found outside tests/fixtures: {rel}")
    if not is_test_or_fixture:
        for address in email_re.findall(data):
            if not allowed_email.fullmatch(address):
                errors.append(f"personal or non-placeholder email found: {rel}")
                break

print(f"Public export: {files} files, {total} bytes ({total / 1024 / 1024:.2f} MiB)")
if errors:
    print(f"Scan result: FAIL ({len(errors)} finding(s))")
    for error in errors:
        print(f"- {error}")
    raise SystemExit(1)
print("Scan result: PASS (privacy, artifact, and size checks)")
PY
