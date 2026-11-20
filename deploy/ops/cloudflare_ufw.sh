#!/usr/bin/env bash
set -euo pipefail
DRY_RUN=1
ROOT=/
while (($#)); do case "$1" in --apply) DRY_RUN=0;; --dry-run) DRY_RUN=1;; --root) ROOT=$2; shift;; *) echo "usage: $0 [--dry-run|--apply] [--root PATH]" >&2; exit 2;; esac; shift; done
IPS="$(cd "$(dirname "$0")/.." && pwd)/nginx/cloudflare_ips.txt"
run() { if ((DRY_RUN)); then printf 'DRY-RUN:'; printf ' %q' "$@"; printf '\n'; else "$@"; fi; }
while IFS= read -r cidr; do
  [[ -z "$cidr" || "$cidr" == \#* ]] && continue
  run ufw allow from "$cidr" to any port 80 proto tcp
  run ufw allow from "$cidr" to any port 443 proto tcp
done < "$IPS"
run ufw deny 80/tcp
run ufw deny 443/tcp
