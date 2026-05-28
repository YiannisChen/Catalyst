#!/usr/bin/env bash
# verify_p0_complete.sh — T-13a through T-16 aggregate checks (read-only on frozen DB).
# Run from repository root: bash docs/defense/verify_p0_complete.sh
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "${ROOT}" ]]; then
  echo "FAIL: not inside a git repository"
  exit 2
fi
cd "${ROOT}"

VENV_PY="${ROOT}/packages/data-core/.venv/bin/python"
if [[ ! -x "${VENV_PY}" ]]; then
  echo "FAIL: venv python missing at ${VENV_PY}"
  exit 2
fi

declare -a RESULTS=()

pass() { RESULTS+=("PASS|$1"); }
fail() { RESULTS+=("FAIL|$1"); }

# 1) Trace counts
nr=0
while IFS= read -r -d '' f; do
  ((nr++)) || true
done < <(find "${ROOT}/data/traces" -maxdepth 1 -name '*.json' ! -name 'rerun_*' -print0 2>/dev/null || true)
rr=0
while IFS= read -r -d '' f; do
  ((rr++)) || true
done < <(find "${ROOT}/data/traces" -maxdepth 1 -name 'rerun_*.json' -print0 2>/dev/null || true)
if [[ "${nr}" -ge 20 && "${rr}" -ge 10 ]]; then
  pass "1 traces non-rerun=${nr} rerun=${rr}"
else
  fail "1 traces non-rerun=${nr} (need ≥20) rerun=${rr} (need ≥10)"
fi

# 2) Tier-A pinning in direct_llm + mcj_full reports
if "${VENV_PY}" -c "
import json
from pathlib import Path
keys = ('model_id_per_role','db_sha256','code_git_sha','random_seed','lancedb_dir_sha256')
root = Path('.')
for pattern in ('data/eval_reports/20260503_154053_direct_llm.json',
                'data/eval_reports/20260503_154053_mcj_full.json'):
    p = root / pattern
    if not p.is_file():
        raise SystemExit(f'missing {pattern}')
    h = json.loads(p.read_text()).get('header', {})
    for k in keys:
        assert k in h, f'{pattern} missing {k}'
" &>/dev/null; then
  pass "2 Tier-A pinning keys in config reports"
else
  fail "2 Tier-A pinning keys in config reports"
fi

# 3) Gate script
COMP="$(ls -t "${ROOT}/data/eval_reports/"*_comparison.json 2>/dev/null | head -1 || true)"
if [[ -z "${COMP}" ]]; then
  fail "3 check_p0_gate (no comparison json)"
else
  if "${VENV_PY}" "${ROOT}/packages/eval/scripts/check_p0_gate.py" "${COMP}" >/dev/null 2>&1; then
    pass "3 check_p0_gate exit 0"
  else
    fail "3 check_p0_gate exit != 0"
  fi
fi

# 4) DB SHA vs comparison header
if "${VENV_PY}" -c "
import json, hashlib, glob
r = json.load(open(sorted(glob.glob('data/eval_reports/*_comparison.json'))[-1]))
freeze = r['header']['db_sha256']
cur = __import__('hashlib').sha256(open('data/catalyst_eval_frozen.db','rb').read()).hexdigest()
assert freeze == cur
" 2>/dev/null; then
  pass "4 DB SHA matches comparison header"
else
  fail "4 DB SHA matches comparison header"
fi

# 5) schema_version 1.0
if "${VENV_PY}" -c "
import json, glob
c = json.load(open(sorted(glob.glob('data/eval_reports/*_comparison.json'))[-1]))
p = json.load(open(sorted(glob.glob('data/eval_reports/preflight_*.json'))[-1]))
assert c.get('schema_version') == '1.0'
assert p.get('schema_version') == '1.0'
" 2>/dev/null; then
  pass "5 schema_version 1.0 comparison+preflight"
else
  fail "5 schema_version 1.0 comparison+preflight"
fi

# 6) §J.6 from task/T-14-close (no packages/ changes after)
if git rev-parse --verify task/T-14-close >/dev/null 2>&1; then
  T14="$(git rev-parse task/T-14-close)"
  V6="$(
    git log "${T14}"..HEAD --name-only --pretty=format: 2>/dev/null |
      grep -v '^$' | grep -E '^packages/' | sort -u || true
  )"
  if [[ -z "${V6}" ]]; then
    pass "6 §J.6 no packages/ after task/T-14-close"
  else
    V6_ONELINE="$(echo "${V6}" | tr '\n' ';')"
    fail "6 §J.6 packages/ after T-14: ${V6_ONELINE}"
  fi
else
  fail "6 task/T-14-close tag missing"
fi

# 7) defense-freeze tag annotated
TAG="$(git tag --list 'defense-freeze-*' | sort | tail -1 || true)"
if [[ -z "${TAG}" ]]; then
  fail "7 no defense-freeze-* tag"
elif [[ "$(git for-each-ref "refs/tags/${TAG}" --format='%(objecttype)' 2>/dev/null)" == "tag" ]]; then
  pass "7 tag ${TAG} is annotated"
else
  fail "7 tag ${TAG} not annotated"
fi

# 8) Tag commit == comparison code_git_sha
if [[ -n "${TAG}" ]]; then
  export TAG_SHA="$(git rev-parse "${TAG}^{commit}" 2>/dev/null || true)"
  if "${VENV_PY}" -c "
import json, glob, os
r = json.load(open(sorted(glob.glob('data/eval_reports/*_comparison.json'))[-1]))
assert r['header']['code_git_sha'] == os.environ['TAG_SHA']
" 2>/dev/null; then
    pass "8 tag SHA matches comparison code_git_sha"
  else
    fail "8 tag SHA matches comparison code_git_sha"
  fi
else
  fail "8 tag SHA (no tag)"
fi

# 9) Audit rows ≥ 14
AUD="$(ls "${ROOT}/docs/testing"/audit_*.md 2>/dev/null | sort | tail -1 || true)"
if [[ -n "${AUD}" ]]; then
  ROWS="$(grep -cE '^\| T-[0-9]+' "${AUD}" 2>/dev/null || echo 0)"
  if [[ "${ROWS}" -ge 14 ]]; then
    pass "9 audit T-NN rows=${ROWS}"
  else
    fail "9 audit T-NN rows=${ROWS} (need ≥14)"
  fi
else
  fail "9 no audit_*.md"
fi

# 10) Deck slides
SLIDES="$(grep -cE '^## Slide [0-9]+:' "${ROOT}/docs/defense/deck.md" 2>/dev/null || echo 0)"
if [[ "${SLIDES}" -ge 8 ]]; then
  pass "10 deck slides=${SLIDES}"
else
  fail "10 deck slides=${SLIDES}"
fi

# 11) Notebook + no live API URLs
if [[ -f "${ROOT}/notebooks/demo.ipynb" ]]; then
  if grep -qE 'api\.openai|api\.anthropic|claude\.ai' "${ROOT}/notebooks/demo.ipynb"; then
    fail "11 demo.ipynb contains live API URL pattern"
  else
    pass "11 demo.ipynb present, no live API URL patterns"
  fi
else
  fail "11 notebooks/demo.ipynb missing"
fi

echo ""
echo "=== verify_p0_complete summary ==="
printf "%-6s  %s\n" "Result" "Check"
printf "%-6s  %s\n" "------" "-----"
for line in "${RESULTS[@]}"; do
  IFS='|' read -r st msg <<< "${line}"
  printf "%-6s  %s\n" "${st}" "${msg}"
done

FAILS=0
for line in "${RESULTS[@]}"; do
  [[ "${line}" == FAIL* ]] && FAILS=$((FAILS + 1)) || true
done

if [[ "${FAILS}" -gt 0 ]]; then
  echo ""
  echo "Overall: FAIL (${FAILS} check(s))"
  exit 1
fi
echo ""
echo "Overall: PASS"
exit 0
