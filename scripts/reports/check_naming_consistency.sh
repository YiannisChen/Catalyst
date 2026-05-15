#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/yiannischen/Desktop/Catalyst"
TARGET_FILES=(
  "$ROOT/docs/reports/2026-05-15-catalyst-full-debug-analysis.md"
  "$ROOT/docs/reports/2026-05-15-r0-baseline-addendum.md"
  "$ROOT/docs/plans/2026-05-15-r1-external-baseline-spec.md"
  "$ROOT/docs/plans/2026-05-15-r0-r1-r2-remediation-master.md"
)

# Guard rule:
# In remediation context, do not use system phase labels P0/P1/P2.
# System phase labels P0/P1/P2 are allowed outside remediation/整改 context.

violations=0
for file in "${TARGET_FILES[@]}"; do
  if [[ ! -f "$file" ]]; then
    echo "[missing] target file not found: $file"
    violations=$((violations + 1))
    continue
  fi
  while IFS= read -r line; do
    lc="$(printf '%s' "$line" | tr '[:upper:]' '[:lower:]')"
    if [[ "$lc" == *"remediation"* || "$line" == *"整改"* ]]; then
      if [[ "$line" == *"P0"* || "$line" == *"P1"* || "$line" == *"P2"* ]]; then
        echo "[violation] $file :: $line"
        violations=$((violations + 1))
      fi
    fi
  done < "$file"
done

if [[ "$violations" -gt 0 ]]; then
  echo "[fail] naming consistency violations: $violations"
  exit 1
fi

echo "[ok] naming consistency guard passed"
