# Catalyst daily updater

The public interface uses five results: **Confirmed**, **Reported**, **With its group**, **No clear cause**, and **Ordinary move**. The fact checker is the second model check; the editor applies fixed code rules before publishing. The daily universe contains 102 large-cap U.S. stocks.

## Deploying code

`/opt/catalyst/repo` is the data/daily checkout; API uses separate `/opt/catalyst/api` and `/opt/catalyst/api/venv`. Deploy from the Mac with `deploy/deploy.sh [ref]`. It pushes the selected commit as `refs/heads/deploy` and `refs/heads/api`, updates both checkouts and records the data commit in `/opt/catalyst/DEPLOYED`. Untracked stores under `data/m9` are not replaced by checkout, but `.gitignore` does not blanket-ignore `*.sqlite3`; never stage a server store or corpus file. Never edit code on the server.

Install/update the units on the server and enable timers only after the required stores and corpus have passed their checks. For a first deployment, keep the daily timer stopped until the API and data checks below pass.

```sh
sudo cp deploy/api/catalyst-headline-archive.service deploy/api/catalyst-headline-archive.timer \
  deploy/api/catalyst-daily.service deploy/api/catalyst-daily.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable catalyst-headline-archive.timer catalyst-daily.timer
```

Both services read `/opt/catalyst/.env`. Keep `SEC_USER_AGENT`, `POLYGON_API_KEY`,
and any other private provider settings there. The daily service runs from
`/opt/catalyst/repo`, uses `/opt/catalyst/venv`, prefers IPv4, sends the configured
Benzinga hosts through the local relay at `127.0.0.1:8765`, and sets
`PYTHONPATH=/opt/catalyst/repo/packages/data:/opt/catalyst/repo/packages/agents`.

The public API reads its model prices and per-role token bounds from the
checked-in `packages/api/catalyst_app/runtime/pricing.json` by default. Set
`CATALYST_PRICING_FILE` to a file path only when an explicit override is
needed; missing, malformed, or incomplete pricing fails public admission with
`MODEL_PRICE_UNAVAILABLE` and makes `/api/health` report `failed`.

Check timer and service state with:

```sh
systemctl list-timers catalyst-daily.timer
systemctl status catalyst-daily.service
journalctl -u catalyst-daily.service
journalctl -u catalyst-daily.service --since today
```

The headline archive runs every day at 10:30 UTC, recording only complete UTC days through yesterday and sharing
`CATALYST_HEADLINE_ARCHIVE_DB` (or the daily runtime data directory default) with the daily job. Its service is
ordered before `catalyst-daily.service`. The daily service runs `python -m catalyst_data.ingestion.ops.daily scheduled`
Tuesday to Saturday at 11:00 UTC, the morning after each session. The scheduled command holds the daily job lease while it collects
prices, filings, movers, news, categories, and images, so manual and timed
updates do not overlap.

After a successful daily run, `ExecStartPost` try-restarts `catalyst-api.service` so it binds the newly published build. The API remains bound to its startup build until that restart.

## Daily ready-range and corpus FTS post-processing

After a completed daily update, the job refreshes the `ready_range.json` snapshot by default. Corpus and FTS5
rebuilding is opt-in and remains skipped unless `CATALYST_CORPUS_FTS_REBUILD=1`. When enabled, the job requires
explicit corpus and Alpaca archive paths; it does not infer either path from the repository. The corpus publish
step is atomic: an unsuccessful build leaves the current build selected. It checks free space before opening the
corpus for writing and skips the build below `CATALYST_CORPUS_MIN_FREE_GB` (default `10` decimal GB). To enable
the step on the server, set these three non-secret variables in the daily service environment (normally
`/opt/catalyst/.env`):

- `CATALYST_CORPUS_FTS_REBUILD=1`: explicitly enable rebuilding.
- `CATALYST_CORPUS_DB=/opt/catalyst/repo/data/m9/catalyst_corpus.sqlite3`: served corpus path.
- `CATALYST_ALPACA_NEWS_DB=/opt/catalyst/archive/alpaca_news.sqlite3`: Alpaca archive path; the daily job opens it read-only.

`CATALYST_CORPUS_MIN_FREE_GB` is required for `seed_corpus.sh --execute`; use `10` GB as the recommended value. Available space must also cover 1.2 times
the prior current generation's chunk plus FTS text bytes. Once a new build is current, older generations and
incomplete staging builds are retired and purged; the current and previous published generations are retained.
`CATALYST_CORPUS_VACUUM=1` optionally compacts the database after pruning; it is off by default. The following
status and ready-range snapshot variables remain optional:

- `CATALYST_DAILY_PIPELINE_STATUS`: optional status JSON path; by default it is
  `$CATALYST_DATA_DIR/daily_pipeline_status.json` (or `data/m9/daily_pipeline_status.json` in the repository).
- `CATALYST_READY_SNAPSHOT`: optional ready-range snapshot path, supported by the existing snapshot builder.

The daily summary includes `ready_snapshot`, the last `corpus_fts` result, and the current `corpus_fts_attempt`.
An attempt skipped because the build is already published, the lock is held, the build is not selected, or the
daily run is incomplete leaves the last FTS result unchanged. A failure in either real post-processing step does not fail the data
collection job; it is also exposed as `daily_pipeline` in `health.json` and sent through the existing tier-1
monitor alert path. If `CATALYST_DAILY_PIPELINE_STATUS` is overridden, configure the same path for
`catalyst-health.service` so the health report reads that file.
The corpus and rollback commands use an adjacent `.fts.lock` file to serialize generation changes across processes;
the manual update endpoint checks the same lock before starting.

To switch selection to a previous published corpus and lexical build without deleting either build, list the
builds read-only first and then name the target explicitly (the command writes the store; see the rollback steps
in the deploy-day section for the full procedure and checks):

```sh
python -m catalyst_data.corpus.rollback --db /opt/catalyst/repo/data/m9/catalyst_corpus.sqlite3 --to <build_id>
```

Without `--to` it selects the previous published build. The command prints the current build before and
after the switch. A `ready_snapshot` alert means the latest prepared-range snapshot may be stale; a `corpus_fts`
alert means search may be serving the previous corpus build (or no new build because the disk threshold was hit).

## Environment and network notes

- `/opt/catalyst/.env` must define `SEC_USER_AGENT` (a descriptive name plus a contact email, as SEC EDGAR requires), `POLYGON_API_KEY` and `FINNHUB_API_KEY`. It may also define `DEEPSEEK_API_KEY` for news categories (classification is skipped if it is missing) and `CATALYST_ADMIN_TOKEN` for `POST /api/data/update`. Keep the file `chmod 600`. Never commit it.
- Set `CATALYST_CORPUS_MIN_FREE_GB=10` as the recommended floor; `seed_corpus.sh --execute` requires this variable. For the API, set `CATALYST_EXPECTED_FTS_DIGEST=served` and `CATALYST_CORPUS_MANIFEST_ID=served` to bind each startup to the currently published corpus build.
- **Benzinga relay.** Benzinga answers 403 to some server IP ranges, including this deployment's VPS. In that case, run the Mac relay plus the reverse tunnel (`python -m catalyst_data.ingestion.collectors.relay`, see `catalyst_data/ingestion/relay.py`). Without it, Benzinga discovery items fail and are retried. A host that Benzinga serves directly can drop the two `CATALYST_RELAY_*` lines from the service.
- **Prices.** yfinance is the primary source. When it returns nothing (Yahoo intermittently blocks some server IPs), sessions are filled from Polygon's grouped daily bars. Both sources are split-adjusted and not dividend-adjusted. They agreed to within 0.01% on about 490 symbols (measured 2026-10-09).

## API deployment

The API uses a separate checkout at `/opt/catalyst/api` and `/opt/catalyst/api/venv`; data, corpus, body database, and curated manifest remain in `/opt/catalyst/repo`, and the body database is not copied. `deploy/deploy.sh` pushes the selected commit to the server's bare `rn` repository and updates both checkouts before calling `deploy_api.sh`. The API unit binds to `127.0.0.1:8000`, uses 1 worker, and reads runtime credentials from `/opt/catalyst/.env` (do not print this file during deployment or checks). The public HTTPS origin is Cloudflare-only on 8443; the web app and API share it. After a successful daily update, the API is try-restarted to bind to the currently served build.

The body database remains read-only through an immutable snapshot by default. The API unit sets `CATALYST_SQLITE_LIVE_READ=1` to read the live database during daily updates using ordinary `mode=ro`. `CATALYST_SQLITE_READ_TIMEOUT_S=10` sets the total wait limit per query, and `CATALYST_SQLITE_READ_RETRIES=4` limits the number of backoff retries after lock conflicts. SQLite's busy timeout and application-level backoff jointly limit the wait; a timeout returns `SQLiteReadTimeoutError`. Body, market, and jobs reads that already used ordinary `mode=ro` explicitly use live mode. Without the switch, the generic `storage.connect` still uses immutable mode, and evaluation snapshot calls retain their existing behavior. `ready_range()` reads a fixed snapshot validated against its manifest and hash; it does not use the live database. In live mode, SQLite still needs to create `-shm`/`-wal` files in the database directory. The API and daily job must run as the same system user (currently root on the server), or as members of the same group with write access to the data directory.

Recommended resource limits are conservative: 1 worker, `MemoryMax=2G`, `CPUQuota=100%` (about one core), `TasksMax=256`, `IOWeight=10`, and `Nice=10`. These lower the API's priority when it competes with the daily job for CPU and disk. The server has 4 cores and about 5.8 GB of memory. Recheck the limits against the machine before deployment: keep MemoryMax at or below 25% of total memory, and reserve at least 50% for the system, SQLite cache, and daily job. Set CPUQuota using `min(100% × core count, 25% × core count × 100%)` and check daily-job latency. Estimate TasksMax as `1 + workers × (1 + 40 + headroom)` (with 1 worker, 40 AnyIO threads, and process headroom, the current limit is 256).

The Nginx configuration template is `deploy/nginx/catalyst-api.conf`. It serves the web app on 8443 with the Cloudflare Origin certificate, proxies `/api/` to loopback port 8000, and serves hashed assets with immutable caching. The `cloudflare_ips.txt` list comes from Cloudflare's official IP ranges: <https://www.cloudflare.com/ips/>. The repository list is only a known snapshot; **manually check the official page before deployment**. `generate_cloudflare_include.py` emits `set_real_ip_from` directives and an http-context `geo $realip_remote_addr` map; all peers outside Cloudflare are rejected with status 444. API traffic is limited at 30 requests/second with burst 60; only POST live-run creation and cancellation also use 2 requests/minute with burst 3. SSE streams disable proxy buffering and use a 300-second read timeout. The request body limit is 1 MiB. By default, `cloudflare_ufw.sh` only prints UFW commands; this topology keeps UFW untouched and does not use it as the peer allowlist.

Health checks are a hard requirement for enabling the API: `sec_exhibit_units.status` must be `ok` and `rows > 0`. Otherwise, confirm the source of the deployed body database and check the exhibit unit metadata; do not expose the API. Before installation, the existing unit, main Nginx configuration, and generated Cloudflare include are saved under `/opt/catalyst/api-deploy-backups/<UTC-timestamp>/`. If `nginx -t` fails, the files are restored. After installation, use `deploy/deploy_api.sh --rollback` to restore the most recent backup. The script does not delete backups.

Pre-deployment checks:

1. Prepare the Cloudflare hostname and origin certificate, the API environment in `/opt/catalyst/.env`, the served corpus, the API and web releases, and the data timers, in that order.
2. Do not expose the API until the corpus and identity-bound runtime prerequisites are verified and `sec_exhibit_units` has nonzero rows.
3. Keep the API deploy backup directory and record the rollback command. If verification fails, run `deploy/deploy_api.sh --rollback` from the deployed repository.

Read-only entry-point check: generic `storage.connect` retains immutable snapshot behavior by default; `CATALYST_SQLITE_LIVE_READ=1` switches the default to the live database. `readiness`, body retrieval, SEC exhibit/amendment-time reads, health body checks, ready snapshot builds, and m9 live database reads that already used ordinary `mode=ro` explicitly use live connections. `read_api.ready_range()` and `storage.readonly_snapshot()` use snapshots fixed by manifest/hash and intentionally do not use live mode. Other local `mode=ro` connections on the market/ingestion side remain short-lived read-only operations. New long-running body database reads must reuse the connection factory.

## Deployment day (data side)

Use this procedure when the data daily job/corpus, API, and agent are switched on the same day. The procedure prepares and transfers the database on the Mac, then validates the database and switches services on the server. Do not expose the API if the body database health requirement fails.

### 1. Check Cloudflare IP ranges before deployment

The `deploy/ops/check_cloudflare_ips.py` script downloads the official `ips-v4` and `ips-v6` lists and compares them entry by entry with `deploy/nginx/cloudflare_ips.txt`. It is read-only by default. If it finds differences, it prints `ADDED`/`MISSING` and exits with a nonzero status. Manually check <https://www.cloudflare.com/ips/> before running `--write`. A write requires at least 10 v4 ranges and 5 v6 ranges; if either list is empty or below the threshold, the script refuses before making a backup or writing. Before writing, it copies the IP list and existing generated include to separate `.bak.*` files with UTC timestamps, then atomically replaces the list and generated include using temporary files in the same directory and `os.replace`. The generated include is installed at `/etc/nginx/conf.d/catalyst-cloudflare-allow.conf`, where Ubuntu loads it in nginx's `http` context. `generate_cloudflare_include.py` validates the number of ranges of each type and writes atomically.

```sh
python3 deploy/ops/check_cloudflare_ips.py
# Run only after manual review:
python3 deploy/ops/check_cloudflare_ips.py --write
```

### 2. Initial corpus copy from the Mac

The served corpus is about 23 GB. `deploy/ops/seed_corpus.sh` defaults to dry-run and transfers only an already-built database matching the script's fixed manifest and FTS identities. There is no empty-database initializer here. The seed script accepts `--host USER@HOST` or `CATALYST_DEPLOY_HOST`, plus `--source`, `--remote-path`, and `--env-file` overrides.

With `--execute`, the script first checks that the server's final file, orphaned sidecars, and all `catalyst_corpus.sqlite3.incoming*` files are absent. Any existing temporary files must be manually renamed and backed up first; the script never overwrites them. It then stops `catalyst-daily.timer` and waits for `catalyst-daily.service` to exit (2 hours by default; adjust with `--wait-timeout-s`). It does not restart the oneshot service. Next, Mac `lsof` checks that no process has the source database or its sidecars open; this is a conservative check, and readers must also exit first. On the source database's filesystem, the script checks for available space of at least 1.2 times the source database size, then creates a temporary copy using SQLite Backup API from a `mode=ro` source connection. The source database is not checkpointed or written. `PRAGMA wal_checkpoint(TRUNCATE)`, validation, hashing, and transfer all operate on the temporary copy. Its `-wal`/`-shm` files must be absent or zero bytes. On the server, opening is refused if any sidecar exists for the incoming file. Using read-only immutable mode, the script validates the copy's full manifest `bf4be0178add29a32bcfda49155325aadb862adc506973a0c9db4be8843c66e7`, the FTS build `f14324922ff6a0618afb2f9cb2f1120b2791f812c2c1072048d8565fa7dcc8e9` in `lexical_index_state`, and the corresponding publication status. SHA-256 is computed for the same temporary copy before and after transfer and must match.

```sh
deploy/ops/seed_corpus.sh --host root@<server-hostname> \
  --source /path/to/catalyst_corpus.sqlite3
# Run after reviewing the dry-run output; optionally add --quick-check for a server-side integrity scan:
deploy/ops/seed_corpus.sh --execute --host root@<server-hostname> \
  --source /path/to/catalyst_corpus.sqlite3
```

Transfer uses `rsync --partial --checksum` to a unique `catalyst_corpus.sqlite3.incoming.<timestamp>` path. The server validates it read-only and verifies the SHA, fixed manifest/build IDs, and publication status before promotion. `--quick-check` is optional. For the historical 22.6 GB size, a 100 MB/s sequential scan has a theoretical lower bound near 4 minutes; total time depends on repeated reads, integrity checking, and the transfer link. The seed script requires free space after copy of at least `CATALYST_CORPUS_MIN_FREE_GB + 1.2 × database size`. The current server facts say 84 GB free, which would leave about 61.4 GB after a 22.6 GB copy before filesystem overhead; recheck `df` at execution time. Promotion uses same-filesystem `mv --no-clobber`, then sets `root:root` and mode `0644`. The seed script does not start the daily timer. After code and health checks, start it explicitly; `Persistent=true` may immediately run missed work. On error, retain the temporary/incoming files and keep the timer stopped until reviewed.

The database transfer/switch window disables `catalyst-daily.timer`. The timer has `Persistent=true`; if the window crosses a scheduled trigger time, systemd may immediately run the missed daily job after the timer is started again. Account for this when scheduling the window. After restoring the timer, check the service status and daily summary. Do not use restart to test it.

After transfer, check the manifest, FTS build, and SHA in the script output. On the server, query the same three tables at the final path in read-only mode and use `df` to confirm available space. The health report `/opt/catalyst/health/health.json` must have a recent `generated_at`, the most recently expected result in `daily_job.status`, and `sec_exhibit_units.status == "ok"` with `sec_exhibit_units.rows > 0`. `sec_exhibit_units` reads the `news_bodies_v2.sqlite3` body database and does not prove that the corpus build is correct. `GET /api/health` is a fast liveness check; `GET /api/health/runtime` performs the blocking runtime dependency readiness check. Neither replaces the read-only corpus build check. The repository has no `/healthz` route.

### 3. Deployment-day checklist

| Order | Role | Action and verification | Rollback on failure |
|---|---|---|---|
| 0. Release ref | Deployment team | Confirm the unified release ref contains the reviewed data, API, and agent versions. D-b rollback/FTS controls must be merged into data/main and deployed first. Record the previous deployment commit. | Do not begin deployment. If code has already been switched, restore the recorded commit using “Code rollback.” |
| 1. Cloudflare | Mac deployment | Run the read-only check script and review added/missing CIDRs; manually check the official page before writing. Before `--write`, verify the minimum v4/v6 counts and confirm timestamped backups were created. Check that the list matches the generated include. | Do not expose Nginx/API. Atomically restore the backup files to the list/include and rerun `nginx -t`. |
| 2. Corpus and maintenance window | Mac deployment | Review source/host/target in the dry-run. `--execute` stops `catalyst-daily.timer`, waits for the service to exit, then creates a backup copy and transfers it. Record the copy's SHA. Check the final database manifest, lexical state, published build, sidecars, free space, and optional `quick_check`. The timer remains stopped after a successful transfer. | On failure, keep `.incoming*` and the local copy, and leave the timer stopped. After confirming the final target is unchanged and the daily job will not create conflicting files, decide whether to restore the timer or retry. If a bad build was published, use corpus rollback. |
| 3. Code switch | Data/API deployment | Switch to the approved data/API code. If `deploy_api.sh` installation fails, it restores the backup. Deploy health unit code from the collectors checkout. This step does not start the daily timer. | Roll back the API with API rollback; roll back data/health code using each checkout's deployment procedure. |
| 4. Health check and agent | API/health/agent deployment | Check that health.json is recent, `daily_job.status` has the expected value, and `sec_exhibit_units.status == "ok"` with `rows > 0`. Verify liveness with `/api/health` and full readiness with `/api/health/runtime`. After they pass, deploy the agent ref and verify the Cloudflare/DNS/HTTPS entry point. If readiness fails, do not expose the API or start the timer. | Keep the API stopped; roll back API, health collectors, and agent through their respective procedures. |
| 5. Start daily job last | Data deployment | Only after the code switch and health checks are complete, run `systemctl start catalyst-daily.timer`. `Persistent=true` may immediately run a missed daily job. Check `systemctl status catalyst-daily.service catalyst-daily.timer`, `daily_job.status/generated_at` in health.json, and `corpus_fts_attempt` in the D-b summary. Verify the current lexical build against the live database using `mode=ro`. | Stop the timer. If the oneshot is running, stop the service as described in the rollback procedure. Disable the next FTS rebuild, then check the current build. |

### 4. Symptom → command → verification / rollback

The corpus rollback CLI and `CATALYST_CORPUS_FTS_REBUILD` switch are available after D-a/D-b is merged into data/main (6b77bb3) and that version is deployed to the server. The D-b `rollback.py` supports the required `--db` and optional `--to` arguments and has no inspection mode; running it writes to the database and switches the build. Therefore, first list builds with a read-only query, then explicitly specify the previous build for rollback. Do not use `$CATALYST_CORPUS_DB`; systemd's EnvironmentFile does not automatically export it to an interactive shell.

First list candidate builds in read-only mode (keep the path absolute when executing the operation):

```sh
/opt/catalyst/venv/bin/python - <<'PY'
import sqlite3
from pathlib import Path
p = Path('/opt/catalyst/repo/data/m9/catalyst_corpus.sqlite3')
db = sqlite3.connect(p.as_uri() + '?mode=ro', uri=True)
try:
    state = db.execute('''SELECT corpus_manifest_id, mode_served, lexical_generation_id
                          FROM lexical_index_state WHERE singleton_id=1''').fetchone()
    print("ACTIVE_LEXICAL_STATE", state)
    for row in db.execute('''SELECT b.build_id, b.manifest_id, b.status, b.lexical_ready,
                                    b.lexical_digest, b.lexical_expected_digest,
                                    b.lexical_row_count, b.chunk_count, m.is_current
                             FROM corpus_publication_builds b
                             LEFT JOIN corpus_manifest m ON m.manifest_id=b.manifest_id
                             WHERE b.status='published'
                             ORDER BY b.published_at DESC, b.created_at DESC,
                                      b.build_id COLLATE BINARY DESC'''):
        print(row)
finally:
    db.close()
PY
```

After verifying and recording the previous `build_id`, run the rollback. `--to` must be a full build ID:

```sh
cd /opt/catalyst/repo
PYTHONPATH=packages/data /opt/catalyst/venv/bin/python -m catalyst_data.corpus.rollback \
  --db /opt/catalyst/repo/data/m9/catalyst_corpus.sqlite3 --to <previous-full-build-id>
```

For the live corpus in use, the verification URI must use `mode=ro` without `immutable=1` so it reads the latest WAL commit. Set `target` in the query below to the full build ID specified in the previous command. Confirm that `lexical_index_state.lexical_generation_id == target`, and that this build is published in `corpus_publication_builds` with an associated manifest that is current. Verify before restarting the API, then run the query again after restart to confirm consistency. Use `immutable=1` only for `.incoming` files that have not yet been published during the copy. `GET /api/health` checks only the runtime database's `runs`/`run_events` and cannot prove that the corpus build was switched back. The `sec_exhibit_units` check in health.json reads the `news_bodies_v2.sqlite3` body database and cannot replace the corpus query above.

```sh
/opt/catalyst/venv/bin/python - <previous-full-build-id> <<'PY'
import sqlite3, sys
from pathlib import Path
target = sys.argv[1]
p = Path('/opt/catalyst/repo/data/m9/catalyst_corpus.sqlite3')
db = sqlite3.connect(p.as_uri() + '?mode=ro', uri=True)
try:
    state = db.execute('''SELECT corpus_manifest_id, mode_served, lexical_generation_id
                          FROM lexical_index_state WHERE singleton_id=1''').fetchone()
    build = db.execute('''SELECT build_id, manifest_id, status, lexical_ready
                          FROM corpus_publication_builds WHERE build_id=?''', (target,)).fetchone()
    manifest = db.execute('SELECT is_current FROM corpus_manifest WHERE manifest_id=?',
                          (build[1],)).fetchone() if build else None
    print('active lexical state:', state)
    print('target build:', build)
    print('target manifest current:', manifest)
    if not state or state[2] != target or not build or build[2] != 'published' \
            or build[3] != 1 or not manifest or manifest[0] != 1:
        raise SystemExit('active corpus does not match target build')
finally:
    db.close()
PY
```

Restart the API only after the live database query passes. Run the same query again after restart to confirm consistency:

```sh
systemctl restart catalyst-api.service
```

| Symptom | Command | Verification |
|---|---|---|
| API issue in the new version; restore pre-install configuration | In the server repository, run `deploy/deploy_api.sh --rollback`. | `nginx -t`; `systemctl status catalyst-api.service`; `curl -fsS http://127.0.0.1:8000/api/health`; read `generated_at` and `sec_exhibit_units.status/rows` in `/opt/catalyst/health/health.json`. |
| Daily FTS rebuild causes an issue | The switch is in `/opt/catalyst/.env` after D-b is deployed. First run `systemctl stop catalyst-daily.timer`; if the oneshot is running, run `systemctl stop catalyst-daily.service`; do not restart it. Edit `.env` to remove the key, then run `systemctl daemon-reload`. The change takes effect at the next service start. | Print only the key count and require it to be 0: `count=$(grep -cE '^[[:space:]]*CATALYST_CORPUS_FTS_REBUILD[[:space:]]*=' /opt/catalyst/.env || true); printf 'key_count=%s\\n' "$count"; test "$count" -eq 0`. Then use a one-off process to check only whether the key is in the environment, without printing other variables: `systemd-run --quiet --wait --pipe --property=EnvironmentFile=/opt/catalyst/.env /opt/catalyst/venv/bin/python -c 'import os; print("CATALYST_CORPUS_FTS_REBUILD=" + ("present" if "CATALYST_CORPUS_FTS_REBUILD" in os.environ else "absent"))'`. It must show `absent` before running `systemctl start catalyst-daily.timer` (do not restart; `Persistent=true` may immediately run a missed job). In the next daily summary, `corpus_fts_attempt` should be `skipped`; check the service log and `/opt/catalyst/repo/data/m9/daily_pipeline_status.json`. |
| A code release causes a failure | `git -C /opt/catalyst/repo checkout <previous-deployment-commit>` changes that checkout only; it does not install unit/Nginx files. Restore the API unit/Nginx with `deploy/deploy_api.sh --rollback`, or reinstall the previous version, then run `systemctl daemon-reload` and `systemctl restart catalyst-api.service`. `catalyst-daily.service` is a oneshot; do not restart it. If it is running, stop it first. After restoring the timer, the next activation uses the previous data code. | Check that the backups in `/opt/catalyst/api-deploy-backups/<UTC-timestamp>/` match `/etc/systemd/system/catalyst-api.service`, `/etc/nginx/conf.d/catalyst-api.conf`, and `/etc/nginx/conf.d/catalyst-cloudflare-allow.conf`. Check `systemctl status catalyst-api.service catalyst-daily.timer`, `nginx -t`, and local `/api/health`. The workspace's `rev-parse HEAD` does not prove that the unit was restored. |
| Health-check code needs rollback | `catalyst-health.service` reads code from `/opt/catalyst/repo/packages/data`. To roll back, stop the health timer, restore the previous deployment commit and saved health unit, then run `systemctl daemon-reload`. Restore the timer after the read-only health check passes. `deploy/deploy_monitor.sh` deploys to the monitoring host at `/opt/catalyst-monitor`, not to this health service. | Run `systemctl cat catalyst-health.service` to confirm ExecStart/PYTHONPATH. After `systemctl start catalyst-health.service`, check `systemctl status catalyst-health.service` and health.json. Then verify the repository checkout/ref. The health.json timestamp proves only that the report was refreshed; it does not prove which repository code it used. |
| Live database read hits a lock or timeout | Do not temporarily remove `CATALYST_SQLITE_LIVE_READ=1`: returning to immutable mode may ignore live database changes and read inconsistent data. First run `systemctl stop catalyst-api.service`, leave the daily job running, and check logs/lock holders. | `systemctl is-active catalyst-api.service` should be inactive; check `systemctl status catalyst-daily.service` and `generated_at` plus `sec_exhibit_units.status/rows` in health.json. Fix the lock issue and pass the health requirement again before starting the API. |
| SEC exhibit health check fails | Do not expose the API. If it is already running, first run `systemctl stop catalyst-api.service`. Check the source of the body database used by the daily job and verify the `sec_exhibit_units` row count. | In `/opt/catalyst/health/health.json`, `sec_exhibit_units.status` must be `ok` and `rows > 0`. Also check `systemctl status catalyst-health.service catalyst-api.service` and local `/api/health`. If the requirement is not met, keep the API stopped. |

Code rollback for the API, data service, collectors health, and agent are separate operations. Before each switch, record the previous commit/build and API deployment backup directory. After rollback, repeat the relevant checks in this section; a command exiting with status 0 does not replace the health requirement.
