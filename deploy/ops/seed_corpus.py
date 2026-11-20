#!/usr/bin/env python3
"""Create an immutable, verified temporary corpus copy and seed it to the server."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = "bf4be0178add29a32bcfda49155325aadb862adc506973a0c9db4be8843c66e7"
BUILD = "f14324922ff6a0618afb2f9cb2f1120b2791f812c2c1072048d8565fa7dcc8e9"
DEFAULT_DB = "/opt/catalyst/repo/data/m9/catalyst_corpus.sqlite3"


def uri(path, immutable=False):
    return Path(path).resolve().as_uri() + ("?mode=ro&immutable=1" if immutable else "?mode=ro")


def validate_db(path):
    conn = sqlite3.connect(uri(path, immutable=True), uri=True)
    try:
        current = conn.execute("SELECT manifest_id FROM corpus_manifest WHERE is_current=1").fetchall()
        if current != [(MANIFEST,)]:
            raise ValueError(f"unexpected current manifest: {current}")
        state = conn.execute("SELECT corpus_manifest_id, mode_served, lexical_generation_id "
                             "FROM lexical_index_state WHERE singleton_id=1").fetchone()
        if state != (MANIFEST, "fts5", BUILD):
            raise ValueError(f"unexpected lexical_index_state: {state}")
        row = conn.execute("SELECT status, manifest_id, lexical_ready FROM corpus_publication_builds "
                           "WHERE build_id=?", (BUILD,)).fetchone()
        if row != ("published", MANIFEST, 1):
            raise ValueError(f"unexpected current build: {row}")
    finally:
        conn.close()


def sidecars(path, allow_empty):
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and (not allow_empty or sidecar.stat().st_size):
            raise RuntimeError(f"unexpected SQLite sidecar: {sidecar}")


def snapshot_source(source):
    size = source.stat().st_size
    available = shutil.disk_usage(source.parent).free
    if available < size * 1.2:
        raise RuntimeError(f"not enough same-disk free space for backup API copy: need {int(size * 1.2)} bytes")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    temp_dir = Path(tempfile.mkdtemp(prefix=f".catalyst-corpus-seed-{stamp}-", dir=source.parent))
    copy = temp_dir / "catalyst_corpus.sqlite3"
    original = sqlite3.connect(uri(source), uri=True, timeout=60)
    destination = sqlite3.connect(copy, timeout=60)
    try:
        original.backup(destination)
        destination.commit()
    finally:
        destination.close()
        original.close()
    clone = sqlite3.connect(copy, timeout=60)
    try:
        result = clone.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result is None or result[0] != 0:
            raise RuntimeError(f"snapshot checkpoint failed: {result}")
    finally:
        clone.close()
    sidecars(copy, allow_empty=True)
    validate_db(copy)
    return copy


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


PREFLIGHT = r'''set -euo pipefail
final=$1
dir=$(dirname "$final")
if [[ -e "$final" || -e "$final-wal" || -e "$final-shm" ]]; then
  echo "TARGET_EXISTS: target or orphan sidecar exists: $final" >&2
  exit 17
fi
for item in "$dir/$(basename "$final").incoming"*; do
  [[ -e "$item" ]] || continue
  echo "INCOMING_EXISTS: $item" >&2
  exit 18
done
[[ -d "$dir" ]] || { echo "target directory missing: $dir" >&2; exit 19; }
'''

PAUSE = r'''set -euo pipefail
wait_seconds=$1
systemctl stop catalyst-daily.timer
echo "Stopped catalyst-daily.timer; waiting for active daily oneshot."
deadline=$((SECONDS + wait_seconds))
while :; do
  state=$(systemctl show catalyst-daily.service -p ActiveState --value)
  case "$state" in inactive|failed) break ;; active|activating|deactivating|reloading) ;; *) exit 22 ;; esac
  if ((SECONDS >= deadline)); then
    echo "daily service still active after $wait_seconds seconds; timer remains stopped" >&2
    exit 23
  fi
  sleep 5
done
echo "catalyst-daily.service is not running."
'''

FINALIZE = r'''set -euo pipefail
incoming=$1; final=$2; expected_sha=$3; size=$4; env_file=$5; quick=$6
if [[ -e "$incoming-wal" || -e "$incoming-shm" ]]; then
  echo "incoming sidecar exists; refusing immutable validation" >&2; exit 30
fi
actual=$(sha256sum "$incoming" | awk '{print $1}')
[[ "$actual" == "$expected_sha" ]] || { echo "SHA-256 mismatch" >&2; exit 31; }
python3 - "$incoming" <<'PY'
import sqlite3, sys
from pathlib import Path
manifest="bf4be0178add29a32bcfda49155325aadb862adc506973a0c9db4be8843c66e7"
build_id="f14324922ff6a0618afb2f9cb2f1120b2791f812c2c1072048d8565fa7dcc8e9"
p=Path(sys.argv[1]).resolve()
for s in ("-wal","-shm"):
    if Path(str(p)+s).exists(): raise SystemExit(f"incoming sidecar exists before open: {s}")
db=sqlite3.connect(p.as_uri()+"?mode=ro&immutable=1",uri=True)
try:
    rows=db.execute("SELECT manifest_id FROM corpus_manifest WHERE is_current=1").fetchall()
    if rows != [(manifest,)]: raise SystemExit(f"wrong current manifest: {rows}")
    state=db.execute("SELECT corpus_manifest_id,mode_served,lexical_generation_id FROM lexical_index_state WHERE singleton_id=1").fetchone()
    if state != (manifest,"fts5",build_id): raise SystemExit(f"wrong lexical state: {state}")
    row=db.execute("SELECT status,manifest_id,lexical_ready FROM corpus_publication_builds WHERE build_id=?",(build_id,)).fetchone()
    if row != ("published",manifest,1): raise SystemExit(f"wrong publication build: {row}")
finally: db.close()
PY
if [[ "$quick" == 1 ]]; then
  python3 - "$incoming" <<'PY'
import sqlite3,sys
from pathlib import Path
p=Path(sys.argv[1]).resolve()
db=sqlite3.connect(p.as_uri()+"?mode=ro&immutable=1",uri=True)
try:
    rows=db.execute("PRAGMA quick_check").fetchall()
    if rows != [("ok",)]: raise SystemExit(f"quick_check failed: {rows[:10]}")
finally: db.close()
PY
else
  echo "PRAGMA quick_check skipped (optional)."
fi
if [[ -e "$incoming-wal" || -e "$incoming-shm" ]]; then echo "incoming sidecar before mv" >&2; exit 32; fi
actual=$(sha256sum "$incoming" | awk '{print $1}')
[[ "$actual" == "$expected_sha" ]] || { echo "incoming SHA changed during checks" >&2; exit 33; }
min_free=$(python3 - "$env_file" <<'PY'
import re,sys
from pathlib import Path
pattern=re.compile(r"^\s*CATALYST_CORPUS_MIN_FREE_GB\s*=\s*['\"]?([0-9]+(?:\.[0-9]+)?)['\"]?\s*(?:#.*)?$")
for line in Path(sys.argv[1]).read_text().splitlines():
    m=pattern.match(line)
    if m: print(m.group(1)); break
else: raise SystemExit("CATALYST_CORPUS_MIN_FREE_GB missing from env file")
PY
)
avail=$(df -PB1 --output=avail "$(dirname "$incoming")" | tail -n 1 | tr -d ' ')
python3 - "$avail" "$min_free" "$size" <<'PY'
import sys
avail,minimum,size=int(sys.argv[1]),float(sys.argv[2]),int(sys.argv[3])
if avail < minimum*1_000_000_000 + 1.2*size: raise SystemExit("insufficient post-copy free space")
PY
if [[ -e "$final" || -e "$final-wal" || -e "$final-shm" ]]; then echo "final target appeared" >&2; exit 34; fi
mv --no-clobber -- "$incoming" "$final"
[[ ! -e "$incoming" && -f "$final" ]] || { echo "no-clobber promotion failed" >&2; exit 35; }
chown root:root "$final"
chmod 0644 "$final"
echo "Promoted verified corpus. After code switch and health checks, operator must run: systemctl start catalyst-daily.timer"
echo "catalyst-daily.timer is Persistent=true and may immediately run missed daily work."
'''


def remote(host, script, *args, capture=False):
    command = "bash -s -- " + " ".join(shlex.quote(str(arg)) for arg in args)
    return subprocess.run(["ssh", host, command], input=script, text=True,
                          capture_output=capture, check=False)


def check_remote_target(host, db):
    result = remote(host, PREFLIGHT, db, capture=True)
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()
        if result.returncode in (17, 18):
            raise RuntimeError(f"remote target/incoming exists; rename it to a backup first:\n{detail}")
        raise RuntimeError(f"remote preflight failed ({result.returncode}): {detail}")


def check_local_open_files(source):
    if shutil.which("lsof") is None:
        raise RuntimeError("lsof is required for source open-file checks")
    for path in (source, Path(str(source)+"-wal"), Path(str(source)+"-shm")):
        if path.exists():
            result = subprocess.run(["lsof","-t",str(path)],text=True,capture_output=True,check=False)
            if result.stdout.strip(): raise RuntimeError(f"source is open: {path}: {result.stdout.strip()}")


def dry_run(source, host, db, env_file, quick, wait_s):
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    incoming=f"{db}.incoming.{stamp}"
    snapshot=source.parent / f".catalyst-corpus-seed-{stamp}-DRY-RUN" / "catalyst_corpus.sqlite3"
    print(f"DRY-RUN source read-only: {source}")
    print(f"DRY-RUN host: {host}; unique incoming: {incoming}")
    print(f"DRY-RUN ssh preflight: final/sidecars and all {db}.incoming* must be absent.")
    print("DRY-RUN lsof source/sidecars.")
    print(f"DRY-RUN ssh systemctl stop catalyst-daily.timer; wait up to {wait_s}s for catalyst-daily.service; "
          "repeat target/incoming preflight.")
    print("DRY-RUN check same-disk 1.2x free space; create same-filesystem copy with SQLite backup API "
          "(source URI mode=ro).")
    print("DRY-RUN run PRAGMA wal_checkpoint(TRUNCATE) on the copy only; verify copy sidecars absent or zero bytes; "
          "immutable read-only checks of corpus_manifest, lexical_index_state, and corpus_publication_builds.")
    print(f"DRY-RUN full identifiers: manifest={MANIFEST}; lexical build={BUILD}.")
    print("DRY-RUN SHA-256 copy; execute rsync --partial --checksum -av -- "
          f"{shlex.quote(str(snapshot))} {shlex.quote(host + ':' + incoming)}; SHA-256 copy again and compare.")
    print("DRY-RUN remote sha256sum; refuse any incoming sidecars; mode=ro&immutable=1 checks of "
          "corpus_manifest, lexical_index_state, and corpus_publication_builds.")
    print("DRY-RUN " + ("run" if quick else "optional") + " PRAGMA quick_check; check free >= "
          "CATALYST_CORPUS_MIN_FREE_GB + 1.2x database size.")
    print("DRY-RUN sidecar recheck; mv --no-clobber; chown root:root; chmod 0644; leave timer stopped.")
    print("代码切换和健康检查完成后，由操作者执行 systemctl start catalyst-daily.timer；"
          "Persistent=true 会立即补跑错过的日更。")
    print(f"DRY-RUN env-file argument (remote read only with --execute): {env_file}")
    print(f"DRY-RUN on later failure, leave timer stopped; retain temp copy under {source.parent}.")
    print("DRY-RUN: no source, timer, server, or network access performed.")


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute",action="store_true",help="execute; default is dry-run")
    parser.add_argument("--host",default=os.environ.get("CATALYST_DEPLOY_HOST"))
    parser.add_argument("--source",type=Path,default=Path(os.environ.get("CATALYST_CORPUS_DB",ROOT/"data/m9/catalyst_corpus.sqlite3")))
    parser.add_argument("--remote-path",default=DEFAULT_DB)
    parser.add_argument("--env-file",default="/opt/catalyst/.env")
    parser.add_argument("--quick-check",action="store_true")
    parser.add_argument("--wait-timeout-s",type=int,default=7200)
    args=parser.parse_args(argv)
    if not args.host: parser.error("--host or CATALYST_DEPLOY_HOST is required")
    if not re.fullmatch(r"[A-Za-z0-9._@:-]+",args.host): parser.error("unsupported host characters")
    if any(ch.isspace() for ch in args.remote_path+args.env_file): parser.error("remote paths cannot contain whitespace")
    if args.wait_timeout_s<=0: parser.error("--wait-timeout-s must be positive")
    if not args.execute:
        dry_run(args.source,args.host,args.remote_path,args.env_file,args.quick_check,args.wait_timeout_s)
        return 0
    source=args.source.expanduser().resolve()
    if not source.is_file(): raise RuntimeError(f"source database not found: {source}")
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    incoming=f"{args.remote_path}.incoming.{stamp}"
    check_remote_target(args.host,args.remote_path)
    check_local_open_files(source)
    stopped=remote(args.host,PAUSE,str(args.wait_timeout_s))
    if stopped.returncode: raise RuntimeError("daily timer stop/wait failed; timer remains stopped")
    check_remote_target(args.host,args.remote_path)
    copy=snapshot_source(source)
    copy_sha=sha256(copy)
    size=copy.stat().st_size
    print(f"Snapshot retained at {copy}; SHA-256={copy_sha}")
    check_remote_target(args.host,args.remote_path)
    copied=subprocess.run(["rsync","--partial","--checksum","-av","--",str(copy),f"{args.host}:{incoming}"],check=False)
    if copied.returncode: raise RuntimeError(f"rsync failed ({copied.returncode}); timer remains stopped")
    sidecars(copy,allow_empty=True)
    if sha256(copy)!=copy_sha: raise RuntimeError("snapshot changed during transfer; timer remains stopped")
    result=remote(args.host,FINALIZE,incoming,args.remote_path,copy_sha,str(size),args.env_file,
                  "1" if args.quick_check else "0")
    if result.returncode: raise RuntimeError(f"remote validation failed ({result.returncode}); timer remains stopped")
    print(f"Verified corpus promoted; timer started; snapshot retained at {copy}.")
    return 0


if __name__=="__main__":
    try: raise SystemExit(main())
    except (OSError,sqlite3.Error,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        print(f"seed_corpus failed: {type(exc).__name__}: {exc}",file=sys.stderr)
        raise SystemExit(1)
