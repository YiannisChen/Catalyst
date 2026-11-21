#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
WEB_DIR="$REPO_ROOT/apps/web"

: "${CATALYST_DEPLOY_HOST:?set CATALYST_DEPLOY_HOST to USER@HOST}"
: "${VITE_TURNSTILE_SITE_KEY:?set VITE_TURNSTILE_SITE_KEY for the public widget}"

command -v rsync >/dev/null || { echo "rsync is required locally" >&2; exit 1; }
command -v ssh >/dev/null || { echo "ssh is required locally" >&2; exit 1; }

stamp=$(date -u +%Y%m%dT%H%M%SZ)
release="web-$stamp"
incoming="/opt/catalyst/releases/.incoming-$release"
destination="/opt/catalyst/releases/$release"

cd "$WEB_DIR"
CATALYST_PUBLIC_BUILD=1 VITE_TURNSTILE_SITE_KEY="$VITE_TURNSTILE_SITE_KEY" npm run build
[[ -s dist/index.html ]] || { echo "build did not produce dist/index.html" >&2; exit 1; }

ssh "$CATALYST_DEPLOY_HOST" bash -s -- "$incoming" <<'REMOTE_PREPARE'
set -euo pipefail
incoming=$1
install -d -m 0755 /opt/catalyst/releases
if [[ -e /opt/catalyst/web && ! -L /opt/catalyst/web ]]; then
  echo "/opt/catalyst/web exists and is not a symlink; refusing to replace it" >&2
  exit 1
fi
if [[ -e "$incoming" ]]; then
  echo "incoming release already exists: $incoming" >&2
  exit 1
fi
install -d -m 0755 "$incoming"
REMOTE_PREPARE

rsync -a --delete "$WEB_DIR/dist/" "$CATALYST_DEPLOY_HOST:$incoming/"

ssh "$CATALYST_DEPLOY_HOST" bash -s -- "$incoming" "$destination" "$release" <<'REMOTE_PUBLISH'
set -euo pipefail
incoming=$1
destination=$2
release=$3
[[ -s "$incoming/index.html" ]] || { echo "uploaded release has no index.html" >&2; exit 1; }
if [[ -e "$destination" ]]; then
  echo "release already exists: $destination" >&2
  exit 1
fi
mv -- "$incoming" "$destination"
link="/opt/catalyst/web.next-$release"
ln -s "releases/$release" "$link"
mv -Tf -- "$link" /opt/catalyst/web

mapfile -t old_releases < <(
  find /opt/catalyst/releases -mindepth 1 -maxdepth 1 -type d -name 'web-*' -printf '%f\n' \
    | sort -r | tail -n +4
)
for old in "${old_releases[@]}"; do
  rm -rf -- "/opt/catalyst/releases/$old"
done
printf 'published %s -> %s\n' /opt/catalyst/web "$release"
REMOTE_PUBLISH
