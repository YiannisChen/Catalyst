# 数据说明（Data Card）

> 下文点名、但已不在工作区的历史文档，正文在 git 历史，标签 `research-archive-20261106`。

> 本卡记录冻结后端版本与评测；正文中 `m9` 的 Python 源码路径是这些历史版本的名称。当前数据包使用 `catalyst_data.ingestion`，可执行命令见 §3。


- **版本**：v2.0.2，2026-10-24（补正 SEC 索引时间说明和 data/main 的未部署边界；v2.0.1：2026-10-18）
- **适用对象**：后端三个冻结版本（`backend-freeze-v1`、`-v2`、`-v2.1`）及其评测
- **原则**：原始数据**不进 git**（`.gitignore` 覆盖 `data/m9/*.sqlite3`、`data/research/`、`data/prospective/stores/` 等）。仓库只提供抓取脚本、数据清单和下文的哈希。
- **哈希**：sha256 只列前 12 位，是 2026-10-16 在本机计算的。SQLite 快照必须只读打开（`mode=ro&immutable=1`），否则哈希会变。
- **许可**：各数据源的许可以提供方条款为准，下文只摘要，不构成法律意见。

## 1. 数据源

### 1.1 日线行情（OHLCV）

| 项 | 内容 |
|---|---|
| 来源 | **服务器日更与前瞻评测**：yfinance 为主，取不到时用 Polygon grouped daily 补当日（`packages/data-core/catalyst_data/m9/market.py`，补的行带 `source='polygon'`）。**研究快照**（E 系列）：Polygon grouped daily（`scripts/research/backfill_grouped_daily.py`） |
| 许可 | Polygon 免费层：个人、非商业使用。Yahoo / yfinance：遵守 Yahoo 条款，仅限个人研究 |
| 覆盖 | 服务器日更：受支持股票池加参照 ETF（SPY、板块 ETF、SMH、USO、UNG、GLD、SLV、CPER）。研究快照：全美股与 ETF〔R: data.grouped_daily〕 |
| 时间语义 | 日级，交易日 D 的数据在 D+1 早上才完整可得（免费层当晚拿不到）。两个来源都做了拆股调整，**没有做分红调整**；分红由 §1.2 的修正处理。两者的收盘价一致性见〔R: data.price_agreement〕 |
| 已知缺陷 | 免费层不提供盘中数据。个别代码可能缺行情：2026-10-15 冒烟测试中 BNY 没有可算的 z，当天跳过。Yahoo 间歇性屏蔽部分服务器 IP，所以需要 Polygon 回补 |
| 存储 | `data/m9/market_context.sqlite3`（在用）、`data/research/grouped_daily.sqlite3`（研究） |
| 快照 | `market_context.v6snap_20261013.sqlite3`：`c045aa645e81…`（测试集运行使用）；`grouped_daily.sqlite3`：`b1df4904df0a…` |

### 1.2 分红与拆股

| 项 | 内容 |
|---|---|
| 来源 | `packages/data-core/catalyst_data/m9/corp_actions.py`，结果存 `data/m9/corp_actions.sqlite3`（环境变量 `CATALYST_CORP_ACTIONS_DB`） |
| 时间语义 | 按除权日修正收益（`groups._market_z` 等计算都会读取） |
| 已知缺陷 | 冻结前加入，没有在测试集上单独评估 |

### 1.3 SEC 8-K 及附件

| 项 | 内容 |
|---|---|
| 来源 | SEC EDGAR submissions 接口与附件页（`m9/sec.py`、`m9/sec_earnings.py`；T6 补抓：`scripts/research/t6_sec_intake.py`） |
| 许可 | 美国政府公开数据。必须遵守 SEC 公平访问政策：请求带 `SEC_USER_AGENT`（名称加联系邮箱，只存 `.env`，**不进 git**），并控制频率 |
| 覆盖 | 服务器日更：股票池内每只股票的 8-K 附件。研究日历：〔R: data.sec_calendar〕（`data/research/events.sqlite3`） |
| 时间语义 | `m9/sec_filings.py` 用 submissions JSON 枚举 accession 与 Items，再以每份申报索引页的 `Accepted` 时间为准，存为 UTC；`acceptanceDateTime` 只保留为候选原值并由 `sec.acceptance()` 对照。**不用 `filingDate`**：17:30 之后接收的文件会记到下一个工作日。`sec_filings.py` 的索引页时间改动目前只在 `data/main`，尚未部署到评测或日更环境 |
| 已知缺陷 | 离线快照原本没有收录非基准日的 8-K。T6 第一轮因此有大量案例没有证据〔R: t6.first_pass_no_evidence〕，补抓后才重跑（T6 报告） |
| 辅助文件 | `data/m9/sec_company_tickers.json`（`a1d4b030a746…`）。公司名匹配依赖它，**必须放在正文库旁边**，否则标题必须写出股票代码才能匹配 |
| 快照 | `events.sqlite3`：`162ab0be90e6…`；T6 正文库副本 `news_bodies_v2.t6.sqlite3`：`2254e1c4db45…` |

### 1.3a SEC 8-K 索引与附件单元（第二版、第二版增补）

| 项 | 内容 |
|---|---|
| 8-K 索引 | `catalyst_data/m9/sec_filings.py`：每个发行人读取 SEC submissions JSON，并按其 `filings.files[]` 补取历史分页文件。JSON 提供 accession 与 Item；每份申报的 SEC 索引页 `Accepted` 是最终受理时间，按 CIK/accession 缓存在 `CATALYST_SEC_SUBMISSIONS_DIR`（默认 `data/m9/sec_submissions/`）的 `CIK{cik}-{accession}-index.json`。缺少索引缓存时离线模式失败关闭；在线模式需 `SEC_USER_AGENT` 并遵守 SEC 请求间隔。`company(ticker)` 取公司名 |
| 在线与离线 | 在线时，缓存超过 6 小时重新抓取（需要 `SEC_USER_AGENT`），抓取失败退回缓存；`CATALYST_SEC_OFFLINE=1` 只读缓存，使回放确定。开发集、T6 与新财报日样本的运行都离线；第二轮前瞻评测在线，缓存放在该轮的数据目录下 |
| 用途 | 第二版：财报锚定（本公司 EARNINGS 事件以 Item 2.02 受理时间为准）、财报直通的触发条件（窗口内的 Item 2.02，以及有无其他重大 8-K）。第二版增补：申报事实的字段与核对 |
| 已知缺陷 | submissions JSON 的 `recent` 部分只列最近约 1,000 份申报，很早的申报需要另取历史文件。目前没有用到历史文件，这已造成漏答：新财报日样本 WFC 2025-10-14 的财报 8-K 早于 `recent` 段最早的一份（2025-10-16），锚定、财报直通和附件入库都看不到它，这一例拒答（运行情况处理规格 缺口 RG-05）。共同申报人的附件 URL 可能带另一个 CIK（如 URI），所以核对用 accession，不用 URL 里的 CIK |
| 附件单元 | `catalyst_data/m9/sec_structure.py`：Ex-99 按 HTML 块（与 `sec.exhibit_text` 相同的最内层 p/div/li/td）分成标题、要点、段落。存证据库表 `sec_exhibit_units`（`url` 主键、`accession`、`units_json`、`parsed_at`），**正文、分块和标题表都不变**，只有规则 `filing` 读取 |
| 单元的写入 | 生产入库：`sec.rows_for_filing` 在行里带上 `structure`，`store.write_assets` 写表。已有库：`scripts/v2/sec_units_backfill.py <库>` 从 EDGAR 重取 HTML 补写（附件不会改变）；`sec_structure.reclassify` 按当前的标题规则重分已存单元，不需要重取 |
| 标题规则 | `is_title`：一行，20–240 字符；没有句号；不以连接词或标点结尾；含 reports / announces / delivers / posts / declares 等公告类动词 |
| 已知缺陷 | 多行标题不被识别，例如 CoStar 把标题分成三行；块结构异常的版式可能产生错误单元。标题判定是启发式，在 T6 上调过两次（后端设计 §5.3、v2.1），T6 因此只算开发数据 |
| 快照（含单元） | 开发集库副本 `news_bodies_v2.v6snap_20261013u.sqlite3`：`6c34df7fbf4e…`（1,206 份附件有单元）；T6 库副本 `news_bodies_v2.t6u.sqlite3`：`50cbe263a9e7…`（1,440）；新财报日样本库 `news_bodies_v2.e100.sqlite3`：`05756a485d49…`（1,317；以开发集库为底，补入 100 个会话的 8-K 附件 131 份，`scripts/v2/sec_intake.py`）。三者都在 v2.1 分支的工作目录 `data/m9/` 下 |

### 1.3b 归档：财报日历与新闻稿 RSS（2026-10-20 起，只收集，归因流程不读取）

| 项 | 内容 |
|---|---|
| 代码 | `data/collectors` 分支：`catalyst_data/m9/earnings_calendar.py`、`catalyst_data/m9/press_rss.py`；服务器上单独的检出目录 `/opt/catalyst/collectors` 和定时器（`deploy/deploy_collectors.sh`），不碰日更任务 |
| 财报日历 | Finnhub `/calendar/earnings`，每天 12:15 与 22:15 UTC 取“今天 −7 到 +28 天”，每次抓取都追加保存（`fetched_at`），所以能查“某个时点日历上写的是什么”（`as_of`）。单次请求最多返回 1,500 条，所以按 3 天一段请求，满额就再对半拆分 |
| 财报日历的限制 | 免费层**没有历史**：2026-07 以前的日期全部为空，只能取到最近约一个半月和未来的日程。归档从 2026-10-20 开始，T6 和新财报日样本无法对照。`hour` 字段（bmo 盘前 / amc 盘后 / dmh 盘中）在 S&P 100 的 85 次财报中填了 69 次（81%） |
| 新闻稿 RSS | PR Newswire（全部新闻稿、财报两个订阅源）与 Business Wire（全部新闻），每 5 分钟取一次。只存订阅源里的标题、链接、发布时间、摘要，以及从中识别出的股票代码，并记下第一次看到的时间（`first_seen_at`）；**从不抓新闻稿正文页**。PR Newswire 全部新闻稿那个源一次只给 20 条，约覆盖 20 分钟，所以要 5 分钟一次 |
| 新闻稿的条款 | 负责人决定按个人、非商业研究用途接入。PR Newswire 的条款限制自动访问和把其内容存入数据库，Business Wire 允许读取 RSS，但不允许存储或汇总其信息；所以只存元数据、低频读取、**不分发**，开源时不包含任何归档数据。可能的后果是被封 IP 或收到停止通知。GlobeNewswire 的 robots.txt 拒绝程序访问，**不接入** |
| 时间语义 | 新闻稿：`published_at` 为通讯社的发布时间（用于交易日归属），`first_seen_at` 为我们看到的时间（用于“当时能看到什么”）。RSS 只列最近的条目，**不能回补**，归档从开始轮询那天算起 |
| 记账 | 每次请求一行 `intake_log`（OK / EMPTY / ERROR / DISABLED），归档里的空档可以查出来 |

### 1.3c 归档：免费新闻源（2026-10-21 起，只收集，归因流程不读取）

| 项 | 内容 |
|---|---|
| 代码 | `data/collectors` 分支 `catalyst_data/m9/news_archive.py`；服务器 `/opt/catalyst/archive/news_archive.sqlite3`，四组定时任务（`deploy/collect/catalyst-collect-news-*`） |
| 原则 | 拿不到历史的来源也接：从开始轮询那天起每天积累。每条只存元数据（标题、链接、时间、来源自带的摘要、股票代码），记下第一次看到的时间 `first_seen_at`；每次请求记一行账（OK / EMPTY / ERROR / DISABLED） |
| 每 15 分钟 | Finnhub 市场新闻（general、merger）；Investing.com 的 4 个 RSS（股市、公司、经济、财报） |
| 每小时 | GDELT（每家公司一条查询、最近 2 小时，每 10 秒 1 次请求，标题提到公司才打上代码）；Seeking Alpha 按代码的 RSS；Alpha Vantage 新闻与情绪（需 `ALPHAVANTAGE_API_KEY`，免费层每天 25 次）；NewsAPI（需 `NEWSAPI_KEY`） |
| 每 2 小时 | Marketaux（需 `MARKETAUX_API_KEY`，每次 20 只股票） |
| 每天 4 次 | Finnhub 公司新闻，股票池全部 101 只（00:30、06:30、18:30、22:30 UTC，避开日更任务和评测时段） |
| 核对后不接 | Yahoo Finance、Google News、CNBC 的 RSS（robots.txt 禁止）；MarketWatch 订阅源、SEC 全文检索（robots.txt 返回 403）；Nasdaq（拒绝脚本访问）；GlobeNewswire（robots.txt 拒绝） |
| 条款 | 个人、非商业研究用途；Seeking Alpha 与 Investing.com 的 robots.txt 允许读取这些订阅源，只存元数据、不分发 |
| 时间注意 | Investing.com 的 RSS 发布时间不带时区，按 UTC 存，需要核对 |

### 1.3d 归档：行情类与事件日历（2026-10-21 起，只收集，归因流程不读取）

- 库文件 `/opt/catalyst/archive/market_archive.sqlite3`，代码 `catalyst_data/m9/market_archive.py`。
- 四张表：`minute_bars`（1 分钟线，含盘前盘后）、`option_snapshots`（财报前的期权链）、`rating_changes`（评级和目标价变动）、`events`（FOMC、BLS、BEA、S&P 成分调整、期权到期日）。
- 每一行都带抓取时间；评级和事件另有 `first_seen_at`。读取时按“截止时间之前已经看到的”过滤。
- 只能作为【数据】事实或候选背景，不能作为原因；接进归因属于 B 类。来源、额度和条款见 `provider_survey.md`。
- 分钟线（同日修订）：Alpaca SIP 为主，yfinance、Polygon 补缺。按月一个文件，放在 `/opt/catalyst/archive/minute/minute_YYYY-MM.sqlite3`；本地滚动保留 90 天，月份文件备份一次、香港拉取一次（runbook §6）。记账仍在 `market_archive.sqlite3`。

### 1.3e 归档：Alpaca 新闻（Benzinga，2026-10-21 起，只收集，归因流程不读取）

- 库文件 `/opt/catalyst/archive/alpaca_news.sqlite3`，代码 `catalyst_data/m9/alpaca.py`，每小时取最近 90 分钟。
- 回补：`alpaca news-backfill --dates …`，按纽约自然日整天取；历史可以到 2015 年。
- 每篇文章记下 `created_at`（发布时间，精确到秒，UTC）、`first_seen_at`，以及每个出现过的 `updated_at`（`news_versions`，RG-17 第一步，只记录、不剔除）。
- 正文取自接口的 `content`。和库里已有的 Benzinga 正文比对，相似度中位数 0.95（`provider_survey.md` §15.1）。

### 1.4 新闻标题与正文

| 项 | 内容 |
|---|---|
| 来源 | Finnhub 公司新闻标题归档（`m9/headline_archive.py`）、Polygon 新闻、免费正文抓取（`m9/fetch.py`，遵守 robots.txt，每个主机每秒 1 次）、Benzinga / Kitco 发现（`m9/benzinga.py`、`m9/kitco.py`）、CNBC 收盘综述（`m9/cnbc_roundups.py`）；研究用 GDELT（E9） |
| 许可 | Finnhub 免费层：个人使用。正文只抓取条款允许自动访问的站点：StockStory、PR Newswire、AOL、GuruFocus 的条款禁止自动抓取，没有接入（归因质量分析）。服务器对 Benzinga 的访问通过本机中继 |
| 覆盖 | 服务器日更：每个异动股（\|涨跌\| ≥ 2%）当日的本公司标题、正文、同行、代理 ETF 和市场新闻 |
| 归档调度 | `catalyst-headline-archive.timer` 每日 10:30 UTC 归档完整 UTC 日期，截止昨天；标题归档与日更共用 `CATALYST_HEADLINE_ARCHIVE_DB`（默认日更运行目录下的 `headline_archive.sqlite3`），随后日更于 11:00 UTC 运行。 |
| 时间语义 | 出版时间。标题只作线索，正文才进入证据 |
| 已知缺陷 | 语料以标题为主，正文覆盖有限。有些评论站点（Motley Fool、24/7 Wall St. 等）会进入发布器的证据（见后端设计 §8） |
| 存储与快照 | `news_bodies_v2.v6snap_20261013.sqlite3`：`5b47c4e793e9…`；`headline_archive.v6snap_20261013.sqlite3`：`4fcec7a8cbdc…`（测试集运行使用） |

### 1.5 Tavily 联网搜索

| 项 | 内容 |
|---|---|
| 来源 | Tavily Search API（当前代码：`packages/data/catalyst_data/ingestion/collectors/web_discovery_v2.py`、`packages/data/catalyst_data/ingestion/collectors/web_search.py`）。发现缓存与单次 web search 是独立入口；事件发现代理的旧联网模式已退役 |
| 许可 | 按 Tavily API 服务条款。结果缓存只用于本地研究 |
| 覆盖 | 只在构建发布器第二阶段的联网缓存时调用（前瞻运行器与 CLI，每只股票最多 4 个额度；服务器每日上限 50）；发布器运行中只读缓存、从不检索（运行情况 S10）。域名白名单为 `web_search.FINANCE_DOMAINS` 加 `web_discovery_v2._EXTRA_DOMAINS` |
| 时间语义 | 提供方元数据 `published_at`。只有日期的值补成当日 `23:59:59Z` |
| 已知缺陷 | **“仅日期”时间戳**：发布器证据中占 5.0%，全部来自 Tavily，进入发布器时不带“仅日期”标记；其中 19 条 D−1 的记录落在 D 盘前〔R: t1.date_only_share〕〔R: t1.preopen_dminus1〕。**零点时间戳**：4.6% 的结果恰为 `00:00:00 GMT`，实际只有日期，却按分钟精度解析，影响事件发现代理和推断关联通道〔R: t1.tavily_midnight〕。带具体时刻的时间戳，在页面带可匹配元数据的 URL 上与页面时间 1 小时内一致〔R: t1.clock_agreement〕；没有验证 1 小时内的差异是否跨过交易窗口边界 |
| 快照 | `web_search_v6_d2.frozen.sqlite3`：`b83aa5cf432e…`；`web_search_v5_d3.frozen.sqlite3`：`571b58b23cab…`（测试集运行使用） |

### 1.6 宏观与政策数据（发布器不使用）

| 项 | 内容 |
|---|---|
| 来源 | FRED / ALFRED（DGS2、DGS10、VIX、美元、原油及历史版本，`scripts/research/fetch_factor_data.py`）、Federal Register API、EIA；FOMC 决议日期写在 `m9/structured_facts.py` |
| 许可 | FRED：遵守圣路易斯联储的使用条款；Federal Register 与 EIA 是美国政府公开数据 |
| 用途 | E7 / E8 研究，以及事件发现代理的结构化事实 |
| 时间语义 | 发布日加当时版本（ALFRED vintage），不用修订值 |
| 已知缺陷 | `structured_facts.py` 里的 FRED release id 标注为“使用前须核对” |
| 快照 | `data/research/fred.sqlite3`：`8d03273fef39…` |

## 2. 清单与股票池

| 文件 | 用途 | sha256 / 来源 |
|---|---|---|
| `data/prospective/sp100_universe_v1.json` | 前瞻评测股票池：S&P 100 成分股，101 个代码 | `be12e4e05f0e…d9d22`（全值见 `prospective.py::UNIVERSE_SHA256`）。来源是 Wikipedia 成分股表 revid 1378987012（2026-10-07）。iShares OEF 持仓 CSV 无法用脚本下载，所以改用这个表；BRK-B 条目中的 HTML 注释已去除〔R: pro.universe〕 |
| `data/manifests/peer_map_v1.json` | 同行与板块映射 | `079e20ebc557…` |
| `data/manifests/issuer_domains_v1.json` | 公司自有域名（事件发现代理的 S 级来源） | `c1b5d51bedfb…` |
| `data/manifests/exposure_map_v1.json` | 推断关联通道的暴露映射表（冻结） | `f81034b88e0b…`，冻结提交 `318a2d1` |
| `data/manifests/heldout_candidates_v5.json`、`v6.json` | 开发集与测试集的案例来源 | git 跟踪 |
| `data/research/e100_sample.json` | 新财报日样本（100 例，E11 去掉 T6 后种子 20261018 抽样） | `04e82d039bdc…`；git 跟踪 |
| `data/research/v3_placebo.json` | 安慰剂日 | `7ebe6b0106466d9c…`（全值见 事件发现代理设计 §5.2） |

## 3. 获取脚本一览

| 数据 | 脚本 / 命令 |
|---|---|
| 服务器日更（价格、8-K、异动、新闻、分类、图片） | `python -m catalyst_data.ingestion.ops.daily scheduled`（见运维手册） |
| 单个交易日的按需刷新 | `catalyst_data.ingestion.ops.refresh.refresh_session` |
| 研究行情 | `scripts/research/backfill_grouped_daily.py` |
| 研究 8-K 日历 | `scripts/research/build_earnings_calendar.py` |
| 宏观因子 | `scripts/research/fetch_factor_data.py` |
| 联网证据 | `python -m catalyst_data.ingestion.collectors.web_discovery_v2 run …` |
| 前瞻评测 | `scripts/research/prospective.py day D`：会把在用的库复制到 `data/prospective/stores/`，`sec_company_tickers.json` 一并复制 |
| 附件单元回填 | `scripts/v2/sec_units_backfill.py <库>`（v2.1 分支） |
| 新样本的 8-K 入库与索引缓存 | `scripts/v2/sec_intake.py <cases.json> <底库> <新库> <索引目录>…`（v2.1 分支） |
| 前瞻评测第二轮 | `scripts/research/prospective2.py day D`（`~/Catalyst-round2/runner`）：数据目录 `data/prospective2/`；第二版增补那一臂运行前先给本轮正文库补单元 |
| 前瞻评测金标 | `scripts/v2/prospective_gold.py run`（`~/Catalyst-eval`，launchd `com.catalyst.gold`）：只读两轮的 `candidates.json`，写 `data/gold/<round>/gold.jsonl` 与哈希账本 |
