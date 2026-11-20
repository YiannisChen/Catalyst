#!/usr/bin/env bash
set -euo pipefail
MODE=install
DRY_RUN=0
ROOT=/
while (($#)); do case "$1" in --rollback) MODE=rollback;; --dry-run) DRY_RUN=1;; --root) ROOT=$2; shift;; *) echo "usage: $0 [--dry-run] [--root PATH] [--rollback]" >&2; exit 2;; esac; shift; done
REPO=$(cd "$(dirname "$0")/.." && pwd)
BACKUPS="$ROOT/opt/catalyst/api-deploy-backups"
UNIT="$ROOT/etc/systemd/system/catalyst-api.service"
NGINX="$ROOT/etc/nginx/conf.d/catalyst-api.conf"
ALLOW="$ROOT/etc/nginx/catalyst-cloudflare-allow.conf"
API_ROOT="$ROOT/opt/catalyst/api"
run() { if ((DRY_RUN)); then printf 'DRY-RUN:'; printf ' %q' "$@"; printf '\n'; else "$@"; fi; }
restore_backup() {
  if [[ -f "$backup/api-commit" ]]; then
    git -C "$API_ROOT" checkout -q --detach "$(cat "$backup/api-commit")"
    "$API_ROOT/venv/bin/pip" install -q -e "$API_ROOT/packages/data[dev]" -e "$API_ROOT/packages/agents" -e "$API_ROOT/packages/api"
  fi
  for name in catalyst-api.service catalyst-api.conf catalyst-cloudflare-allow.conf; do
    case "$name" in catalyst-api.service) dst="$UNIT";; catalyst-api.conf) dst="$NGINX";; *) dst="$ALLOW";; esac
    if [[ -f "$backup/$name.absent" ]]; then
      if [[ -e "$dst" ]]; then mv "$dst" "$backup/failed-$name"; fi
    elif [[ -f "$backup/$name" ]]; then
      [[ ! -e "$dst" ]] || cp -a "$dst" "$backup/failed-$name"
      cp -a "$backup/$name" "$dst"
    fi
  done
}
if [[ "$MODE" == rollback ]]; then
  latest=$(find "$BACKUPS" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort | tail -1)
  [[ -n "$latest" ]] || { echo "no API backup found at $BACKUPS" >&2; exit 1; }
  backup=$latest
  restore_backup
  ((DRY_RUN)) || { systemctl daemon-reload; nginx -t && systemctl restart catalyst-api; }
  exit 0
fi
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup="$BACKUPS/$stamp"
run mkdir -p "$backup"
# deploy.sh updates the API checkout first and passes the commit it replaced.
if [[ -n "${PREV_API_COMMIT:-}" ]]; then
  ((DRY_RUN)) || printf '%s\n' "$PREV_API_COMMIT" > "$backup/api-commit"
fi
for pair in "$UNIT:catalyst-api.service" "$NGINX:catalyst-api.conf" "$ALLOW:catalyst-cloudflare-allow.conf"; do
  src=${pair%%:*}; name=${pair#*:}
  if [[ -f "$src" ]]; then run cp -a "$src" "$backup/$name"; else run touch "$backup/$name.absent"; fi
done
if ((DRY_RUN)); then
  run install -D "$REPO/deploy/api/catalyst-api.service" "$UNIT"
  run install -D "$REPO/deploy/nginx/catalyst-api.conf" "$NGINX"
  echo "DRY-RUN: python3 $REPO/deploy/ops/generate_cloudflare_include.py $REPO/deploy/nginx/cloudflare_ips.txt $ALLOW"
  echo "DRY-RUN: nginx -t; systemctl daemon-reload; systemctl enable catalyst-api; systemctl restart catalyst-api"
  exit 0
fi
rollback_on_error() {
  local status=$?
  trap - ERR
  set +e
  restore_backup
  systemctl daemon-reload
  nginx -t
  systemctl restart catalyst-api
  exit "$status"
}
trap rollback_on_error ERR
install -D "$REPO/deploy/api/catalyst-api.service" "$UNIT"
install -D "$REPO/deploy/nginx/catalyst-api.conf" "$NGINX"
python3 "$REPO/deploy/ops/generate_cloudflare_include.py" "$REPO/deploy/nginx/cloudflare_ips.txt" "$ALLOW"
nginx -t
systemctl daemon-reload
systemctl enable -q catalyst-api
systemctl restart catalyst-api
if ! systemctl is-active --quiet catalyst-api; then false; fi
trap - ERR
