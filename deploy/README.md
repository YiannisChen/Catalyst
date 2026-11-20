# Catalyst daily updater

## Deploying code

`/opt/catalyst/repo` is the data/daily checkout; API uses separate `/opt/catalyst/api` and `/opt/catalyst/api/venv`. Deploy from the Mac with `deploy/deploy.sh [ref]`. It pushes the selected commit as `refs/heads/deploy` and `refs/heads/api`, updates both checkouts and records the data commit in `/opt/catalyst/DEPLOYED`. Untracked stores under `data/m9` are not replaced by checkout, but `.gitignore` does not blanket-ignore `*.sqlite3`; never stage a server store or corpus file. Never edit code on the server.

Install the units on the server and enable the timer only after the backfill job is `COMPLETED`.

```sh
sudo cp deploy/api/catalyst-headline-archive.service deploy/api/catalyst-headline-archive.timer \
  deploy/api/catalyst-daily.service deploy/api/catalyst-daily.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now catalyst-headline-archive.timer catalyst-daily.timer
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

## API 部署

API 使用独立 `/opt/catalyst/api` checkout 和 `/opt/catalyst/api/venv`；数据、corpus、正文库和精选 manifest 仍位于 `/opt/catalyst/repo`，不复制正文库。服务单元见 `deploy/api/catalyst-api.service`，绑定 `127.0.0.1:8000`、1 个 worker，并从 `/opt/catalyst/.env` 读取运行凭据（部署/检查时不要输出该文件）。部署脚本只配置 Nginx，不争用 443；443 由负责人决定。日更成功后会 try-restart API 以重新绑定当前 served build。

正文库默认仍按不可变快照只读；API 单元设置 `CATALYST_SQLITE_LIVE_READ=1`，以普通 `mode=ro` 读取日更中的活库。`CATALYST_SQLITE_READ_TIMEOUT_S=10` 是每条查询的总等待上限，`CATALYST_SQLITE_READ_RETRIES=4` 限定锁冲突后的退避重试次数；SQLite busy timeout 与应用层退避共同限制等待，超时返回 `SQLiteReadTimeoutError`。原本就是普通 `mode=ro` 的正文/市场/jobs 读取显式使用活库模式；通用 `storage.connect` 未设置开关时仍使用 immutable，评测快照调用保持原行为。`ready_range()` 读取经过清单与哈希验证的固定快照，不改为活库。活库模式下 SQLite 仍需在库文件同目录创建 `-shm`/`-wal` 文件；API 与日更必须使用同一系统用户（当前服务器为 root），或同组且数据目录可写。

资源上限采用保守建议值：1 worker、`MemoryMax=2G`、`CPUQuota=100%`（约一核）、`TasksMax=256`、`IOWeight=10`、`Nice=10`，降低 API 与日更争抢 CPU/磁盘的优先级。服务器实测为 4 核、约 5.8 GB 内存；上线前仍按机器资源复核：MemoryMax 建议不超过总内存的 25%，并为系统、SQLite 缓存和日更保留至少 50%；CPUQuota 建议按 `min(100% × 核数, 25% × 核数 × 100%)` 配置并检查日更时延；TasksMax 按 `1 + workers × (1 + 40 + 余量)` 估算（1 worker、40 个 AnyIO 线程并留进程余量时当前上限为 256）。

Nginx 配置模板为 `deploy/nginx/catalyst-api.conf`。`cloudflare_ips.txt` 列表来源为 Cloudflare 官方 IP ranges：<https://www.cloudflare.com/ips/>；该仓库列表只是已知快照，**部署前必须人工核对官方页面**。`generate_cloudflare_include.py` 从同一清单生成 allow 与 `set_real_ip_from` 指令。建议一般 API 限制 30 请求/分钟、burst 20；实时运行 `/api/run` 限制 2 请求/分钟、burst 2；请求体上限 1 MiB，连接/发送/读取超时分别为 5/30/120 秒。`cloudflare_ufw.sh` 默认仅打印 UFW 命令；人工核对 IP 和 SSH 管理访问规则后才可传 `--apply`。

健康检查是启用 API 的硬门槛：`sec_exhibit_units.status` 必须为 `ok` 且 `rows > 0`，否则先确认部署正文库来源并核对附件单元数据，不应开放 API。安装前会在 `/opt/catalyst/api-deploy-backups/<UTC时间戳>/` 保存现有 unit、Nginx 主配置和生成的 Cloudflare include；`nginx -t` 失败会恢复文件。安装后可用 `deploy/deploy_api.sh --rollback` 恢复最近一份备份。脚本不会删除备份。

上线前核对：

1. 确认机器内存/核数并调整 unit 资源上限；确认 `/opt/catalyst/api/venv` 和 `catalyst_app.main:app` 可用。
2. 核对 `.env` 权限与必需变量，不输出变量值；确认只读文件权限允许 `catalyst` 用户读取正文库。
3. 核对 `sec_exhibit_units` 健康状态及行数大于零。
4. 人工核对 Cloudflare 当前 IPv4/IPv6 段、DNS 代理状态、证书路径（替换 `catalyst.example.com`）与源站防火墙策略；先 dry-run UFW 脚本。
5. 执行 `deploy/deploy_api.sh --dry-run` 审阅路径，再由部署负责人安装；检查 `nginx -t`、服务状态和本机 health/API 响应。
6. 保留备份目录并记录回滚命令，验证失败时执行 `deploy/deploy_api.sh --rollback`。

只读入口核对：通用 `storage.connect` 默认保留 immutable 快照行为，`CATALYST_SQLITE_LIVE_READ=1` 可默认切到活库；对原本就是普通 `mode=ro` 的 `readiness`、正文检索、SEC 附件/更正时间读取、health 正文检查、ready snapshot build 及 m9 活库读取均显式使用活库连接。`read_api.ready_range()` 与 `storage.readonly_snapshot()` 操作经过 manifest/hash 固定的快照，刻意不走活库模式。其他市场/采集侧的局部 `mode=ro` 连接仍按短时只读操作使用；新增长时正文库读取必须复用连接工厂。

## 部署日（data 侧）

本节用于 data 日更/检索库与 API、agent 在同一天切换的窗口。先由负责人确认一个已审阅的发布 ref 同时包含要上线的 D-a/D-b 日更 FTS 与回滚代码、D-f API 配置以及 agent 侧批准版本；D-a/D-b 尚未合入时不得把“脚本已写”当作已部署。流程在 Mac 上准备和传库，在服务器上验库和切换。不要让 API 在正文库健康门槛失败时对外开放。

### 1. 部署前核对 Cloudflare IP 段

脚本 `deploy/ops/check_cloudflare_ips.py` 从官方 `ips-v4`、`ips-v6` 下载列表，与 `deploy/nginx/cloudflare_ips.txt` 逐条对比。默认只读，发现差异会打印 `ADDED`/`MISSING` 并以非零退出；先人工核对 <https://www.cloudflare.com/ips/>，确认后才运行 `--write`。写操作要求 v4 至少 10 段、v6 至少 5 段；任一为空或低于门槛都会在备份/写入前拒绝。写入前会把 IP 清单和已有 include 分别复制为带 UTC 时间戳的 `.bak.*` 文件，然后用同目录临时文件和 `os.replace` 原子替换清单与生成 include。`generate_cloudflare_include.py` 自身也校验两类网段数量并原子写入。服务器安装时 `deploy_api.sh` 仍从同一清单生成实际 include。

```sh
python3 deploy/ops/check_cloudflare_ips.py
# 经人工核对后才执行：
python3 deploy/ops/check_cloudflare_ips.py --write
```

### 2. Mac 首次拷贝检索库

检索库当前不在服务器上，源文件约 22.6 GB。`deploy/ops/seed_corpus.sh` 默认 dry-run，只有显式 `--execute` 才访问服务器或读源库。主机必须用 `--host USER@HOST` 或 `CATALYST_DEPLOY_HOST` 提供；不在脚本中固定 IP。可用 `--source`、`--remote-path`、`--env-file` 覆盖源库、服务器目标和服务器环境文件路径。

`--execute` 首先检查服务器正式文件、孤立 sidecar 和所有 `catalyst_corpus.sqlite3.incoming*` 都不存在；已有临时文件必须先人工改名备份，脚本绝不覆盖。随后停止 `catalyst-daily.timer`，等待 `catalyst-daily.service` 退出（最长默认 2 小时，可用 `--wait-timeout-s` 调整），不会重启 oneshot。再用 Mac `lsof` 检查源库和 sidecar 没有进程打开；这是保守检查，读者也必须先退出。脚本在源库同一文件系统先检查至少 1.2 倍源库大小的可用空间，再通过 SQLite Backup API 从 `mode=ro` 的源连接生成临时副本。源库不 checkpoint、不写入。`PRAGMA wal_checkpoint(TRUNCATE)`、校验、哈希和传输全部针对临时副本。副本的 `-wal`/`-shm` 必须不存在或为零字节；服务器 incoming 的任一 sidecar 只要存在就拒绝打开。副本只读 immutable 核验完整 manifest `bf4be0178add29a32bcfda49155325aadb862adc506973a0c9db4be8843c66e7`、`lexical_index_state` 的 FTS build `f14324922ff6a0618afb2f9cb2f1120b2791f812c2c1072048d8565fa7dcc8e9`，以及对应 publication 状态。SHA-256 在传输前后对同一临时副本计算且必须一致。

```sh
deploy/ops/seed_corpus.sh --host root@<服务器主机名> \
  --source /path/to/catalyst_corpus.sqlite3
# 审阅 dry-run 输出后执行；可加 --quick-check 做服务器端完整性扫描：
deploy/ops/seed_corpus.sh --execute --host root@<服务器主机名> \
  --source /path/to/catalyst_corpus.sqlite3
```

传输使用 `rsync --partial --checksum` 到带 UTC 时间戳的新名 `catalyst_corpus.sqlite3.incoming.<时间戳>`；不会使用/覆盖既有 incoming。服务器用 `mode=ro&immutable=1` 核验尚未上线的 incoming，前后均检查无 sidecar，核对 SHA 与本地副本一致、manifest/build 完整字符串完全相同；`--quick-check` 可选。活库核对必须用 `mode=ro` 且不带 `immutable=1`，以读取 WAL 中的最新提交。22.6 GB 在持续 100 MB/s 的理想顺序读取下，单纯扫描下限约 4 分钟，SQLite 校验和机器负载可能延长到数十分钟。拷贝后剩余空间必须不少于 `CATALYST_CORPUS_MIN_FREE_GB + 1.2 × 数据库字节数`。服务器 `/` 最近记录的剩余空间约 86 GB；22.6 GB 拷贝后约 63 GB，仍须按已配置的最小空间阈值核算。全部检查通过才在同盘 `mv --no-clobber` 为正式文件，并设为 `root:root`、`0644`。脚本不启动 timer；代码切换和健康检查完成后，由操作者最后执行 `systemctl start catalyst-daily.timer`。该 timer 为 `Persistent=true`，可能立即补跑错过的日更。出错时保留副本和 incoming，timer 保持停止，需操作人确认安全状态后再启动，不执行 `rm`。

传库/切换窗口会停用 `catalyst-daily.timer`。该 timer 设置了 `Persistent=true`，如果窗口跨过计划触发时间，重新 `start` 后 systemd 可能立即补跑错过的日更；安排窗口时留意这一点，恢复 timer 后检查 service 状态和日更 summary，不要通过 restart 试探。

传库完成后，核对脚本输出中的 manifest、FTS build 与 SHA；服务器上只读查询正式路径的同三张表，并用 `df` 确认可用空间。健康报告 `/opt/catalyst/health/health.json` 至少要有新鲜 `generated_at`、`daily_job.status` 为最近预期结果，以及 `sec_exhibit_units.status == "ok"`、`sec_exhibit_units.rows > 0`。`sec_exhibit_units` 读的是 `news_bodies_v2.sqlite3` 正文库，不能证明检索库 build 正确。实际 API 健康路由是 `GET /api/health`，检查命令为 `curl -fsS http://127.0.0.1:8000/api/health`；该路由检查运行库 `runs`/`run_events`，不检查 corpus build；仓库没有 `/healthz` 路由。

### 3. 部署日检查清单

| 顺序 | 谁做 | 动作与验证 | 失败时回滚 |
|---|---|---|---|
| 0. 定版 | 发布负责人、data/agent 负责人 | 确认统一发布 ref 含已审阅的 data、API、agent 版本；D-b rollback/FTS 开关必须先合入 data/main 并部署；记录上一部署提交。 | 不开始部署；若已切换代码，按“代码回滚”恢复记录的提交。 |
| 1. Cloudflare | Mac 部署执行者 | 运行只读核对脚本，审阅新增/缺失 CIDR；写入前人工核对官方页面。`--write` 前核验 v4/v6 最少条数，并确认带时间戳备份生成；检查清单与生成 include 一致。 | 不开放 Nginx/API；将备份文件原子恢复回清单/include，重跑 `nginx -t`。 |
| 2. 检索库与维护窗口 | Mac 部署执行者，data 负责人复核 | dry-run 审阅 source/host/target；`--execute` 会停止 `catalyst-daily.timer`，等待 service 结束后再做备份副本和传输。记录副本 SHA；检查正式库 manifest、lexical state、published build、sidecar、空间及可选 `quick_check`。传库成功后 timer 仍保持停止。 | 失败时 `.incoming*` 和本地副本保留，timer 保持停止；确认正式目标未变、日更不会创建冲突文件后，再由负责人决定恢复 timer 或重试。若已发布坏 build，使用 corpus rollback。 |
| 3. 代码切换 | data/API 负责人 | 切换已批准的 data/API 代码；`deploy_api.sh` 安装失败会恢复备份。health 单元代码按 collectors checkout 部署。此步骤不启动 daily timer。 | API 用 API rollback；data/health 代码按各自 checkout 的部署路径回滚。 |
| 4. 健康检查与 agent | API、health、agent 负责人 | 核对 health.json 新鲜度、`daily_job.status` 与 `sec_exhibit_units.status == "ok"`、`rows > 0`；本机验证 `curl -fsS http://127.0.0.1:8000/api/health`。通过后部署 agent ref 并按 Cloudflare/DNS/HTTPS 入口验收。健康硬门槛不过时不开放 API，也不启动 timer。 | API 保持 stopped；按 API、health collectors、agent 各自路径回滚。 |
| 5. 最后启动日更 | data 负责人 | 只有代码切换和健康检查完成后，才执行 `systemctl start catalyst-daily.timer`。`Persistent=true` 可能立即补跑错过的日更；检查 `systemctl status catalyst-daily.service catalyst-daily.timer`、health.json 的 `daily_job.status/generated_at` 和 D-b summary 的 `corpus_fts_attempt`，并用活库 `mode=ro` 核验当前 lexical build。 | 停 timer；如 oneshot 在运行，按回滚手册 stop service；先关闭下一次 FTS rebuild，再核对当前 build。 |

### 4. 症状 → 命令 → 验证 / 回滚

corpus rollback CLI 和 `CATALYST_CORPUS_FTS_REBUILD` 开关已随 D-a/D-b 合入 data/main（6b77bb3），部署日把这一版部署到服务器后才能使用下列命令。已核对 D-b 的 `rollback.py` 只支持必需的 `--db` 和可选 `--to`，没有查看模式；运行该 CLI 会写库并切换 build。因此必须先用只读查询列出 build，再显式指定上一 build 回滚。不要用 `$CATALYST_CORPUS_DB`，它不会从 systemd 的 EnvironmentFile 自动进入交互 shell。

先只读查看候选 build（正式执行前将路径保持为绝对路径）：

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

核实并记录上一 `build_id` 后，才运行真正回滚；`--to` 必须是完整 build ID：

```sh
cd /opt/catalyst/repo
PYTHONPATH=packages/data /opt/catalyst/venv/bin/python -m catalyst_data.corpus.rollback \
  --db /opt/catalyst/repo/data/m9/catalyst_corpus.sqlite3 --to <上一完整build_id>
```

对正在使用的活检索库，核对 URI 必须为 `mode=ro` 且不带 `immutable=1`，以读取 WAL 中的最新提交。将下列查询的 `target` 设为上一条命令指定的完整 build ID；必须确认 `lexical_index_state.lexical_generation_id == target`，且 `corpus_publication_builds` 中该 build 已发布、关联 manifest 当前有效。先在 API 重启前核对，重启后再运行一次确认一致。`immutable=1` 仅用于拷贝过程中尚未上线的 `.incoming` 文件。`GET /api/health` 只检查运行库 `runs`/`run_events`，不能证明检索库 build 已切回；health.json 的 `sec_exhibit_units` 检查的是正文库 `news_bodies_v2.sqlite3`，也不能代替上述 corpus 查询。

```sh
/opt/catalyst/venv/bin/python - <上一完整build_id> <<'PY'
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

只有活库查询通过后才重启 API；重启后再次运行上面的同一查询，确认一致：

```sh
systemctl restart catalyst-api.service
```

| 症状 | 命令 | 验证 |
|---|---|---|
| API 新版异常，需要恢复安装前配置 | 在服务器仓库执行 `deploy/deploy_api.sh --rollback`。 | `nginx -t`；`systemctl status catalyst-api.service`；`curl -fsS http://127.0.0.1:8000/api/health`；读取 `/opt/catalyst/health/health.json` 的 `generated_at` 与 `sec_exhibit_units.status/rows`。 |
| 日更 FTS rebuild 导致异常 | 该开关在 D-b 部署后的 `/opt/catalyst/.env`。先 `systemctl stop catalyst-daily.timer`；若 oneshot 正在运行，执行 `systemctl stop catalyst-daily.service`，不要 restart。编辑 `.env` 删除该键，再执行 `systemctl daemon-reload`；变更只对下一次 service 启动生效。 | 只输出键名计数并要求为 0：`count=$(grep -cE '^[[:space:]]*CATALYST_CORPUS_FTS_REBUILD[[:space:]]*=' /opt/catalyst/.env || true); printf 'key_count=%s\\n' "$count"; test "$count" -eq 0`。再用一次性进程只检查该键是否进入环境、不打印其他变量：`systemd-run --quiet --wait --pipe --property=EnvironmentFile=/opt/catalyst/.env /opt/catalyst/venv/bin/python -c 'import os; print("CATALYST_CORPUS_FTS_REBUILD=" + ("present" if "CATALYST_CORPUS_FTS_REBUILD" in os.environ else "absent"))'`；必须显示 `absent` 后才执行 `systemctl start catalyst-daily.timer`（不要 restart；`Persistent=true` 可能立即补跑）。下一次日更 summary 中 `corpus_fts_attempt` 应为 `skipped`，查看 service 日志和 `/opt/catalyst/repo/data/m9/daily_pipeline_status.json`。 |
| 发布代码本身导致故障 | `git -C /opt/catalyst/repo checkout <上一个部署提交>` 只改变该 checkout；它不会安装 unit/nginx 文件。API unit/Nginx 用 `deploy/deploy_api.sh --rollback` 恢复，或按上一版本重新安装，再 `systemctl daemon-reload` 并 `systemctl restart catalyst-api.service`。`catalyst-daily.service` 是 oneshot，不要 restart；若正在运行先 stop，恢复 timer 后下一次 activation 使用旧 data 代码。 | 核对 `/opt/catalyst/api-deploy-backups/<时间戳>/` 中备份与 `/etc/systemd/system/catalyst-api.service`、`/etc/nginx/conf.d/catalyst-api.conf`、`/etc/nginx/catalyst-cloudflare-allow.conf` 一致；检查 `systemctl status catalyst-api.service catalyst-daily.timer`、`nginx -t` 和本机 `/api/health`。工作区 `rev-parse HEAD` 不是 unit 已恢复的证据。 |
| health 检查代码需要回滚 | `catalyst-health.service` 从 `/opt/catalyst/repo/packages/data` 读取代码。回滚时先停 health timer，恢复上一部署提交和保存的 health unit，再执行 `systemctl daemon-reload`；通过只读健康检查后恢复 timer。`deploy/deploy_monitor.sh` 部署的是监控主机 `/opt/catalyst-monitor`，不是该 health service。 | `systemctl cat catalyst-health.service` 确认 ExecStart/PYTHONPATH；`systemctl start catalyst-health.service` 后查 `systemctl status catalyst-health.service` 与 health.json。再核验 repo checkout/ref；health.json 时间戳只证明报告刷新，不证明用了 repo checkout 的代码。 |
| 活库读取报锁/超时 | 不要临时移除 `CATALYST_SQLITE_LIVE_READ=1`：回到默认 immutable 可能忽略活库变化并读到不一致数据。先 `systemctl stop catalyst-api.service`，保留日更运行并查日志/锁持有者。 | `systemctl is-active catalyst-api.service` 应为 inactive；`systemctl status catalyst-daily.service`；health.json 的 `generated_at` 与 `sec_exhibit_units.status/rows`；修复锁问题并重新通过硬门槛后才启动 API。 |
| SEC 附件健康检查未通过 | 不开放 API；若 API 已启动，先 `systemctl stop catalyst-api.service`。核实日更实际使用的正文库来源并核对 `sec_exhibit_units` 行数。 | `/opt/catalyst/health/health.json` 中 `sec_exhibit_units.status` 必须是 `ok` 且 `rows > 0`；再查 `systemctl status catalyst-health.service catalyst-api.service` 和本机 `/api/health`。不满足就保持 API stopped。 |

API、data 服务、collectors health 和 agent 的代码回滚是分开的操作：每次切换前记录上一提交/build 和 API 部署备份目录；回滚后重新通过本节对应验证，不能以“命令退出 0”替代健康门槛。
