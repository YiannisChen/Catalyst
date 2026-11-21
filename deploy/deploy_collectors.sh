#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
O=(-o ControlMaster=no -o ControlPath=none -o ConnectTimeout=20 -o BatchMode=yes)
SSH=${CATALYST_SSH:-ssh}
mode=${1:-}
case "$mode" in
  --code-only|--units|--enable-timers) ;;
  *) echo "usage: $0 --code-only|--units|--enable-timers <ref> [--yes for --units]" >&2; exit 2 ;;
esac
shift
ref=
yes=false
for arg in "$@"; do
  if [[ "$arg" == --yes ]]; then yes=true
  elif [[ -z "$ref" ]]; then ref=$arg
  else echo "unexpected argument: $arg" >&2; exit 2
  fi
done
if [[ -z "$ref" ]]; then echo "a ref is required" >&2; exit 2; fi
cd "$ROOT"
sha=$(git rev-parse --verify "$ref^{commit}")
head=$(git rev-parse --verify 'HEAD^{commit}')
if [[ "$head" != "$sha" ]]; then
  echo "HEAD does not match target ref ($head != $sha)" >&2
  exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "worktree is not clean" >&2
  exit 1
fi
HOST=${CATALYST_DEPLOY_HOST:-}
if [[ -z "$HOST" ]]; then echo "CATALYST_DEPLOY_HOST is required (USER@HOST)" >&2; exit 2; fi
PYTHONPATH="$ROOT/packages/data" python3 -m catalyst_data.ingestion.sources check

remote='set -euo pipefail
'
if [[ "$mode" == --code-only || "$mode" == --enable-timers ]]; then
  remote+="if pgrep -f '[c]atalyst_data\.(ingestion\.(collectors\.)?|m9\.)(alpaca|peer_events)' >/dev/null; then echo 'a collector process is running; deployment refused'; exit 1; fi
"
fi
case "$mode" in
  --code-only)
    git remote get-url rn >/dev/null 2>&1 || git remote add rn "$HOST:/opt/catalyst/catalyst.git"
    GIT_SSH_COMMAND="$SSH ${O[*]}" git push -q rn "+${sha}:refs/heads/collectors"
    remote+="if [[ ! -d /opt/catalyst/collectors/.git ]]; then git clone -q /opt/catalyst/catalyst.git /opt/catalyst/collectors; fi
cd /opt/catalyst/collectors
git fetch -q origin collectors && git checkout -q -f --detach FETCH_HEAD
test \"\$(git rev-parse HEAD)\" = '$sha' || { echo 'checked-out HEAD does not match $sha'; exit 1; }
mkdir -p /opt/catalyst/archive /opt/catalyst/bin /opt/catalyst/health /opt/catalyst/backup
install -m 755 deploy/ops/monitor-gate deploy/ops/catalyst-backup deploy/ops/catalyst-job deploy/ops/catalyst-shadow deploy/ops/shadow_compare.py /opt/catalyst/bin/
echo '$sha $ref '\$(date -u +%FT%TZ) > /opt/catalyst/COLLECTORS_DEPLOYED
cat /opt/catalyst/COLLECTORS_DEPLOYED
"
    ;;
  --units)
    remote+="test \"\$(git -C /opt/catalyst/collectors rev-parse HEAD)\" = '$sha' || { echo 'remote collectors checkout is not $sha; run --code-only first'; exit 1; }
cd /opt/catalyst/collectors
for unit in deploy/collect/catalyst-collect-*.service deploy/collect/catalyst-collect-*.timer deploy/ops/catalyst-health.service deploy/ops/catalyst-health.timer deploy/ops/catalyst-backup.service deploy/ops/catalyst-backup.timer deploy/ops/catalyst-resilience-retry.service deploy/ops/catalyst-resilience-retry.timer; do
  name=\${unit##*/}
  if [[ \$name == catalyst-resilience-retry.* ]]; then echo \"=== \$name (not installed) ===\"; else echo \"=== \$name ===\"; fi
  diff -u \"/etc/systemd/system/\$name\" \"\$unit\" || true
done
"
    if [[ "$yes" == true ]]; then
      remote+="for unit in deploy/collect/catalyst-collect-*.service deploy/collect/catalyst-collect-*.timer deploy/ops/catalyst-health.service deploy/ops/catalyst-health.timer deploy/ops/catalyst-backup.service deploy/ops/catalyst-backup.timer; do cp \"\$unit\" /etc/systemd/system/; done
systemctl daemon-reload
"
    fi
    ;;
  --enable-timers)
    remote+="test \"\$(git -C /opt/catalyst/collectors rev-parse HEAD)\" = '$sha' || { echo 'remote collectors checkout is not $sha; run --code-only first'; exit 1; }
cd /opt/catalyst/collectors
want=\$(PYTHONPATH=packages/data /opt/catalyst/venv/bin/python -m catalyst_data.ingestion.sources timers | deploy/ops/collector_timer_want --stdin)
if [[ -n \"\$want\" ]]; then systemctl enable -q --now \$want; fi
for t in \$(systemctl list-unit-files --plain --no-legend 'catalyst-collect-*.timer' | awk '{print \$1}'); do
  echo \"\$want\" | grep -qx \"\$t\" || { systemctl disable -q --now \$t; echo \"stopped \$t (not in the registry)\"; }
done
if systemctl is-enabled catalyst-resilience-retry.timer >/dev/null 2>&1 || systemctl is-active catalyst-resilience-retry.timer >/dev/null 2>&1; then
  systemctl disable --now catalyst-resilience-retry.timer
  echo 'disabled catalyst-resilience-retry.timer (retry installation requires separate approval)'
fi
echo 'final collector timers:'
systemctl list-timers 'catalyst-collect-*' --no-pager
"
    ;;
esac
"$SSH" "${O[@]}" "$HOST" "$remote"
