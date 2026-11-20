#!/bin/zsh
# Deploy a git ref from this Mac to the RackNerd server.
#   deploy/deploy.sh [ref]   (default: the current branch)
# The server keeps a bare repo at /opt/catalyst/catalyst.git; /opt/catalyst/repo is
# a git checkout of it. The stores under data/m9 are gitignored, so a checkout never
# touches them. The deployed commit is written to /opt/catalyst/DEPLOYED.
set -e
O=(-o ControlMaster=no -o ControlPath=none -o ConnectTimeout=20 -o BatchMode=yes)
ref=${1:-$(git rev-parse --abbrev-ref HEAD)}
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
git remote get-url rn >/dev/null 2>&1 || git remote add rn "$HOST:/opt/catalyst/catalyst.git"
GIT_SSH_COMMAND="ssh ${O[*]}" git push -q -f rn "${sha}:refs/heads/deploy"
GIT_SSH_COMMAND="ssh ${O[*]}" git push -q -f rn "${sha}:refs/heads/api"
ssh $O $HOST "set -e; cd /opt/catalyst/repo
  if pgrep -f '[c]atalyst_data\.(ingestion\.(ops\.)?|m9\.)daily' >/dev/null; then echo 'a daily job is running; deploy refused'; exit 1; fi
  git fetch -q origin deploy && git checkout -q -f --detach FETCH_HEAD
  test -f data/m9/news_bodies_v2.sqlite3 || { echo 'stores missing under data/m9'; exit 1; }
  echo \"$sha $ref \$(date -u +%FT%TZ)\" > /opt/catalyst/DEPLOYED
  cat /opt/catalyst/DEPLOYED"
# The API runs from its own checkout and venv. deploy_api.sh records the
# replaced API commit and rolls the unit and checkout back on failure.
ssh $O $HOST "set -e
  test -d /opt/catalyst/api/.git || git clone -q /opt/catalyst/catalyst.git /opt/catalyst/api
  prev=\$(git -C /opt/catalyst/api rev-parse HEAD 2>/dev/null || true)
  git -C /opt/catalyst/api fetch -q origin api && git -C /opt/catalyst/api checkout -q -f --detach FETCH_HEAD
  test -x /opt/catalyst/api/venv/bin/python || python3 -m venv /opt/catalyst/api/venv
  /opt/catalyst/api/venv/bin/pip install -q -e '/opt/catalyst/api/packages/data[dev]' -e /opt/catalyst/api/packages/agents -e /opt/catalyst/api/packages/api
  PREV_API_COMMIT=\$prev /opt/catalyst/repo/deploy/deploy_api.sh"
