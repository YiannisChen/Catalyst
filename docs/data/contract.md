# 数据契约（data-core 基线 v1.1）

> 下文点名、但已不在工作区的历史文档，正文在 git 历史，标签 `research-archive-20261106`。


| 项 | 内容 |
|---|---|
| 日期 | 2026-10-24 |
| 状态 | **v1.1（2026-10-31）并入 CC2 审核意见 14 条；其中 #2（published 截止点）是唯一不兼容项，已由负责人采纳，见 §10**；v1.0 于 2026-10-30 定稿（负责人宣布）。内容即原草案 v0.3；之后的改动按 v1.1、v1.2… 记录在 §10 版本记录。本状态不代表代码、消费者接入或生产部署已完成；未决的事实清单与消费者联调项见 §9 |
| 范围 | 规定 data 层交给归因的东西：一条证据长什么样、时间怎么定、怎么按时点读、怎么去重、缺口怎么报。归因只通过这份契约读数据，不直接读各个库 |
| 相关 | 清单 · 无人值守运行 · 数据就绪 · 更正对照表 `data/corrections/` |

## 1. 一条证据

| 字段 | 类型 | 说明 |
|---|---|---|
| `evidence_id` | 文本 | 正文：`web:` + sha256(规范化 URL) 的前 32 位（现有规则不变）；标题：`hl:<来源>:<来源内 id>` |
| `kind` | 枚举 | `body`（全文）· `headline`（只有标题和摘要）· `filing`（SEC 申报的附件或正文）· `fact`（代码算出的事实，如行情、财报差值） |
| `tickers` | 列表 | 文章涉及的股票（来源的标注加代码表的公司名匹配）；多类股份见 §7 |
| `title`、`text` | 文本 | `text` 只在 `kind=body/filing` 时有 |
| `url`、`canonical_url` | 文本 | 规范化规则见 §4 |
| `publisher`、`publisher_family` | 文本 | 例：`Benzinga`、`benzinga.com` |
| `provider` | 文本 | 数据从哪里来：`finnhub`、`polygon`、`alpaca`、`sec`、`rss:prnewswire`、`tavily` 等 |
| `source_class` | 枚举 | `issuer_disclosure` · `wire` · `news` · `aggregator` · `market_data` |
| `evidence_role` | 枚举 | 每个来源关联显式登记为 `evidence` 或 `lead`；只作线索的来源不得进入证据列表 |
| 时间字段 | 见 §2 | |
| `origin` | 文本或 null | 原始出版方的稳定标识；同一新闻稿用经确认的新闻稿编号。与 provider 和采集渠道分开；未知或冲突时 `origin` 为 null，`origin_status` 为 `unresolved` 或 `conflict`，不凭标题生成独立来源 |
| `observed_at` | UTC 时间 | 每一正文版本首次被本系统取得的时间；不能用文章 first_seen_at 替代后来版本的 observed_at |
| `association_observed_at` | UTC 时间 | 本系统首次把该版本与该 ticker 关联的时间；observed 模式也必须满足它不晚于 cutoff |
| `provider_updated_at` | UTC 时间或空 | 提供方声明的该版本修改时间；缺失不推定为从未修改 |
| `is_backfill` | 布尔 | 是否历史回补；回补不改变真实 first_seen_at |
| `provenance` | 枚举 | `legacy`（仅限 §3.1 明确登记的迁移队列）或 `versioned`（新归档及后续采集，遵守完整版本规则）；不凭记录日期自动推定 |
| `migration_cohort_id` | 文本或空 | legacy 必填，对应不可变迁移清单、旧 eligible_at 规则版本与数据快照身份 |
| `record_key` | 文本 | 证据记录的组合主键 `(evidence_id, provider, version)` 的稳定文本形式。跨 provider 同一 URL 的两条原始记录各有各的 `record_key`，共存、互不覆盖；`evidence_id` 不变（v1.1） |
| `content_hash` | 文本 | 该版本**原文字节**的 SHA-256。哈希在入库时对取得的原始字节计算，不通过重新清洗正文来伪造旧版本的哈希；缺原文的旧版本此字段为空并保持为空（v1.1） |
| `content_state` | 枚举 | `full_text` · `snippet` · `headline` · `missing`。与 `kind` 分开：有全文不自动等于可作证据（v1.1） |
| `usage_ceiling`、`usage_reason` | 枚举、原因码 | data 给出的最大用途：`evidence` 或 `lead`，以及原因码。上限由代码按 (`provider`、`publisher`、`content_state`) 的登记组合判定，不靠 provider 名字推断；例如只有 provider=alpaca、publisher=Benzinga、content_state=full_text 的组合（§6）可达 `evidence`，并且仍须通过完整性、时点、版本、origin 检查。agents 只能进一步降级，不得把 data 的 `lead` 升为 `evidence`（v1.1） |
| `version` | 整数 | 同一 `evidence_id` 的内容改过时加一（Alpaca 的版本表、页面声明的修改时间）；按时点读时只给截至那一刻的版本 |

## 2. 时间字段

| 字段 | 含义 |
|---|---|
| `published_at` | 发布时间（UTC，带 Z）。按 §2.1 的规则从各来源确定 |
| `published_at_source` | 这个时间是从哪里来的：`sec_index`（EDGAR 索引页）、`api`、`page`（页面声明）、`rss`、`wire_match`（同一稿件在通讯社的时间）、`correction`（更正对照表）、`url_path`（URL 路径里的日期）、`provider_date`（提供方只给日期）。`url_path` 与 `provider_date` 的精度固定为 `day`，这类行**只能是线索**，不得出现 `eligible_at`，原因码 `date_only`；页面或提供方给出带时刻的日期后才可升级（v1.1）。消费者遇到未知的取值按“不可用作证据”处理 |
| `published_at_precision` | `second` · `minute` · `day` · `unknown`。只到天的证据不能用来判断“开盘前 / 盘中 / 收盘后” |
| `first_seen_at` | 我们第一次抓到它的时间（UTC）。observed 模式的硬界限；published 不把本地获取时间冒充历史可得性 |
| `eligible_at` | 判断“是否在会话窗口内”所用的时间。versioned 按 published_at 及本契约时间规则；legacy 的 published 读取保留显式迁移清单固定的原 eligible_at 规则（§3.1），不重新推导历史资格 |
| `time_flags` | 列表：`agree`、`api_offset`（已知的整小时偏差，已更正）、`suspected_tz`（两个时间整 4 或 5 小时不一致，按较晚的入库）、`disagree`、`imprecise`、`corrected:<对照表版本>` |
| `api_published`、`page_published` | 两个原始时间都保留，供审计 |
| `eligible_at_original`、`eligible_at_source` | 更正前的 `eligible_at` 原值和它的来源（v1.1）。更正后的值仍放在 `eligible_at`，并在 `time_flags` 记 `corrected:<版本>`；两者都保留。更正是读取视图，不改写冻结输入：更正表新增、去重关系扩展、映射更新都不改变已有运行的解释，旧运行、重放、检查点恢复读取原来固定的那一份 |
| `time_confirmation` | 闭集 `verified` · `unverified` · `conflict`，并附 `time_confirmation_reason` 和它针对的内容版本（`record_key`）（v1.1）。精度（`published_at_precision`）与确认状态分开。原因码至少区分：`version_observation_missing`（缺版本观察时间）· `after_cutoff`（明确晚于截止）· `modification_unconfirmed`（无法确认修改）· `date_only`（来源时间只有日期）· `timezone_missing`（时区缺失）· `conflict_unresolved`（冲突未解决）。这些不得合并成 EMPTY。现有 `point_in_time_unverified` 保留，对应 `unverified` |

### 2.1 各来源的时间规则

1. **SEC 8-K**：以 EDGAR 申报索引页的 “Accepted”（纽约时间）为准，换算成 UTC。submissions 接口的 `acceptanceDateTime` 只用来初筛候选，因为约一半的发行人在接口里晚整 4 或 5 小时（2026-10-22 核实：226 份中 119 份偏晚，其余相等）。两者相差整 4 或 5 小时记 `api_offset`；其他不一致记 `disagree` 并告警；索引页没有时间的不入库，下次重试。换算必须有夏令时夹具（v1.1）：2026-11-01（美国夏令时结束）当天重复的 01:00–02:00 小时、11-01 前后相邻两天的 UTC 换算、2027-03-14（反方向）。
2. **Finnhub**：CNBC、Benzinga 两个发布方的时间，是把美东时钟当成 UTC 给出的。有页面时间就取接口与页面中较晚的；没有页面时间就记 `imprecise`，不作证据（`times.SKEWED`）。
3. **Alpaca（Benzinga 通讯社）**：`created_at`，到秒；内容有更新时用 `updated_at` 加版本号。
4. **RSS**：`pubDate` 必须带时区，没有时区就记为空，不补默认时区。
5. **页面声明的时间**：只接受带时区的。
6. **更正**：已入库的旧数据不改写，在读取层按更正对照表给出更正后的时间，并在 `time_flags` 里记 `corrected:<版本>`。对照表包括 `time_corrections_v1.csv`（Finnhub 时区）和 `sec_acceptance_v1.csv`（SEC 接收时间）。
7. **新来源**：每个新接入的来源必须在登记表里写明一个独立的对照来源（`time_reference`，有测试检查）。

### SEC 8-K 接收时间更正读取

`catalyst_data.read_api.sec_8k_acceptance_table(db_path, *, tickers=None, start=None, end=None, accessions=None, store_id="server:news_bodies_v2.sqlite3", correction_table_path=None, immutable=False)` 以 SQLite `mode=ro` 查询正文库里的 `daily:sec` 记录。静态只读副本可设 `immutable=True`；正常运行数据库保留默认值，以便 SQLite 读取 WAL。调用方提供 accession 列表，或提供一个/多个 ticker 与含首尾日期的 UTC 日期范围。日期查询先从 SQL 取请求日期前后各一天的候选，应用更正后再按 `corrected_at` 的 UTC 日期精筛。接受时间为空的候选会抛 `SecFilingDataError`，不会被其他记录掩盖。更正按 `sec_acceptance_v1.csv` 的精确 `in_stores` 范围应用。

返回 accession、CIK、ticker、`form`、`filed_date`、`acceptance_at`、`corrected_at`、`correction_status`、`correction_ref`、文档 URL 列表 `exhibits` 和 `missing_fields`。`correction_status` 取 `applied`、`none`、`not_applicable_store`：无对应更正时 `corrected_at` 等于 `acceptance_at` 且 `correction_ref` 为 `none`；更正表有该 accession 但不适用于当前 store 时，`corrected_at` 为 `null`、`correction_ref` 仍指向表行，并在 `missing_fields` 标记 `corrected_at`。日期查询会保留此类落在扩展候选范围内的记录，因为其更正后日期未知。无匹配本地记录或接受时间缺失时抛 `SecFilingDataError`；这表示本地数据不足，不表示 SEC 没有申报。

当前 `daily:sec` 投影只保存 accession、ticker、文档 URL 与接收时间，不独立保存申报日期或 8-K/8-K/A 表单值。因此 `filed_date` 与 `form` 返回 `null` 并列入 `missing_fields`；不能从接受日期或展示标题推断这两个字段。该接口枚举已存的 SEC 正文记录，不保证日期范围覆盖完整申报清单。

## 3. 按时点读取

```
as_of(ticker, window_start, window_end, at, *, mode, kinds=None) -> ReadResult   # published 下 at 可省略
```

- `mode` 是必填参数，取 `observed` 或 `published` 之一；每次读取的返回值和每次运行都要记录所用的 `mode`。历史结果一律标注为“回溯查询”。
- **`observed`（严格模式；正式服务实时查询与前瞻评测使用，调用方必须显式指定）**：只返回在 `at` 时刻确实已知的版本和 ticker 关联。条件：`version.observed_at <= at`、`first_seen_at <= at` 且 `association_observed_at <= at`。没有任何放宽的回退：以上任一时间晚于 `at`，或关联时间未知，均不能作为该时点证据返回，**也不进线索列表**，只在审计里留原因码（v1.1 澄清，现有实现已是如此）；只有“已观察到、但发布精度或确认不足”的才降为线索。历史数据缺版本级观察时间的，该版本完全不进 evidence 和 leads，不伪造观察时间。边界夹具：`window_start` 开边界、`window_end` 闭边界、`first_seen_at = observed_as_of` 的等号。来源角色为 `lead` 的记录只能进入独立线索列表；其余来源资格依照 §6 显式标注，不从 provider 字段推定。
- **`published`（回溯查询模式）按 provenance 分流**：
  - `legacy`：仅对 §3.1 明确登记的旧队列保留原有 `eligible_at`、会话窗口、来源角色与既有排除规则；不因缺少 `provider_updated_at` 单独降为线索，也不追溯施加新版本存储规则。仍明确显示 legacy，不能宣称已经证明历史正文未被修改。原规则已有的修改时间检查继续有效。
  - `versioned`：新归档及后续采集要求 `published_at <= at`；正文/申报作为证据还要求已知的 `provider_updated_at <= at` 且 `evidence_role=evidence`。截止后修改的版本只作线索；修改时间未知的回补内容标记 `point_in_time_unverified`，不能作为已经核验时点的证据。`evidence_role=lead` 的记录只能进入线索列表。
- **published 的截止点是阶段 `window_end`（v1.1，负责人 10-31 采纳）**：`at` 省略时取 `window_end`；`at` 晚于 `window_end` 时抛错，不放宽也不静默改用 `window_end`。因此 `window_end` 之后被改写的正文不作证据返回（降为线索，原因 `modified_after_cutoff`）。observed 下 `observed_as_of`（即 `at`）与 `window_end` 是两个量，都要传，`eligible_at` 的窗口条件不能换成 `first_seen_at`。
- 两种模式共同的条件：`tickers` 含该股；`window_start < eligible_at <= window_end`；版本取该模式下允许的最后一个。published 的 legacy 分支按迁移清单固定的旧规则计算 eligible_at，其余按本契约；legacy 标签不豁免 observed 的任何时间条件。
- `eligible_at` 为空的证据不返回。线索用另一个函数 `leads()` 返回，明确标为“只作线索”。
- 读取跨所有库：日更库、归档、证据库。对调用方隐藏各库的差别。
- 复现必须同时固定数据快照/版本、更正表版本、契约与去重规则版本。尤其 published 模式后来新增历史回补会改变可见集合，不能仅凭同一组时间参数承诺结果永久相同。读取结果的信封与审计记录包含 mode、cutoff、实际数据身份、更正表身份、provenance 和 migration_cohort_id；混合结果逐条标记并报告各 provenance 数量，不能把 legacy 混入“时点已核验”集合；正文列表与线索列表分开。
- **published 下当前正文被改过、缺截止前旧版本**时，不得用当前正文顶替：返回线索或未知并给原因码；重放缺旧正文时报错 `VERSION_BODY_UNAVAILABLE`，不静默降级（v1.1）。
- **边界**：本契约只规定 data 层的元数据；app / agents 如何记录和使用 `mode`，等评测结束（2026-11-13）后由各自的负责人接入，不在本契约内实现。
- **版本行的存储**：每个版本行必须完整保留正文、内容哈希、`observed_at`、`provider_updated_at`。现有旧 `news_versions` 表只存时间戳和长度，因此当前快照本身不能重建被覆盖的旧正文；备份恢复尚未核实，不能据此宣称所有副本都无法恢复。受影响数量与可从备份恢复的数量须在获准的只读快照核查后填入清单（inventory），不得编造。

### 3.0 读取信封与可复现身份（v1.1）

每次读取返回一个可序列化的身份，调用方原样存进运行清单：

- 契约版本、`mode`、`observed_as_of`、窗口、快照建立时刻（墙钟，不进证据哈希）、可选 `snapshot_id`；
- 更正表：版本 + 完整 SHA-256 + 适用规则版本，未启用也要显式写 `enabled=false`，不用空串；
- 去重：规则版本，实际用到的 `same_article` / `same_release` 关系集合（不可变快照）及哈希；
- 发行人身份：ticker/CIK 历史映射版本、哈希、适用有效日期；来源角色登记表版本与哈希；
- `migration_manifest_sha256`（第三版两臂用同一份迁移清单；legacy 判据不按文章日期或结果事后决定）；
- 实际证据：有序的 `record_key`、`version`、`content_hash`、`first_seen_at`/`observed_at`、`eligible_at`/精度/`time_confirmation`、`origin`、`usage_ceiling`。

三个哈希分开：`evidence_set_hash`（覆盖有序的证据**和线索**，两个集合分开、各自按固定顺序，不丢 lead 成员）、`data_read_identity_hash`（覆盖上面所有身份）、`model_input_hash`（agents 自己算，data 不管）。序列化规则双方固定：UTF-8、键按字典序、`null` 写 `null`、UTC 时间 `YYYY-MM-DDTHH:MM:SSZ`、证据排序键 `(eligible_at, store_id, source_id, document_id, version_id)`、拒绝 NaN。只给哈希不够，恢复和重放必须能取回对应的不可变内容。

### 3.1 legacy 的显式迁移边界（负责人最新决定）

- 允许登记的队列：已有开发集、T6、新财报日样本，以及明确盘点的既有日更正文。这里的“新财报日样本”指现有样本集合，不是未来每个财报日的数据。
- 迁移清单须逐项固定库/快照的路径与 SHA-256、表与记录身份（或可复现的身份集合）、原 eligible_at 规则版本、采集与修改时间字段的实际存在情况，并分配 migration_cohort_id。仅有“迁移前”“某日期以前”或目录名称不能构成队列边界。
- 不得将迁移前已经存在的新归档（例如 Alpaca 归档）自动整体标成 legacy，也不得将未来回补到旧日期的文章归入 legacy。任何未被显式清单列入的归档与后续采集均走 versioned 规则。
- 不补造 first_seen_at、observed_at 或 provider_updated_at。observed 下不能证明在 cutoff 前已知的旧版本不返回；published 下的 legacy 保留旧资格规则，同时显著标注“回溯查询 · legacy：沿用旧数据资格规则，历史版本完整性未核验”。
- 旧版本正文在当前快照中缺失的事实保留；备份恢复情况尚未核实，迁移不会凭长度或时间元数据重建正文。上述各队列的条目数、缺修改时间数、已覆盖旧版本影响数与备份恢复情况均为**待盘点**，当前没有完整实测结果，不引用推测数字。
- 本节是兼容性设计，不改变冻结评测数据或现行归因路径；agent/app 如何展示、记录并使用这些字段，由负责人协调对齐。

## 4. 跨来源链接与去重（2026-10-23 修订，取代原先的标题加 10 分钟窗口规则）

- **原则**：原始记录一律保留，不做删除；`same_article` / `same_release` 链接只是把记录连起来，供去重后的计数使用。
- **`same_article`（同一篇文章）**：`evidence_id` 按 provider 划分作用域，链接的依据是：
  1. 规范化后的 URL / 文章身份一致（规范化规则：小写主机名；去跟踪参数和片段；AMP 映射到原页）；
  2. 同一 provider 内的记录 ID。
- **`same_release`（同一份新闻稿/发布）**：依据是经确认的新闻稿编号，或者确认一致的完整正文指纹（规范化规则和版本固定）。SEC accession 只标识申报，不单独证明不同附件是同一新闻稿；相同通稿电头或相同正文开头也不够。
- **标题相似度只产生候选链接**：必须再用正文指纹或来源出处（wire 编号、同一发布 ID）确认，才能建立链接。只有标题相似、没有正文或出处证据的，保持为独立记录。
- **独立报道不合并**：不同来源对同一事件的独立报道（各自撰写）不是同一稿件，data 层不做合并；“多个独立来源”的计数按 `origin` 计算。
- **`origin` 字段**：表示原始发布方或发布 ID（如 Benzinga 稿件经 Alpaca 与 Finnhub 进来，`origin` 都是原始出版方 Benzinga，不能为每篇文章生成一个新的独立来源；一份新闻稿经 PR Newswire、IR、8-K EX-99.1 进来，`origin` 都是那份新闻稿）。agent 计数“几个来源”时按 `origin` 数。
- **关系记录（v1.1）**：每条 `same_article` / `same_release` 关系带规则版本、匹配依据、**关系发现时刻**、成员 `record_key`。observed 下关系、发行人映射、来源角色都按 `observed_as_of` 过滤（后来发现的关系不能提前生效）；published 下用固定快照并按样本交易日核验映射有效期；两种模式都固定实际采用的集合与哈希（§3.0）。
- **`origin` 的分配**：同一份新闻稿经 IR、通讯社、SEC 8-K EX-99.1 进入，`origin` 是同一个；origin 未知或冲突时 `origin` 为 null，`origin_status` 为 `unresolved` 或 `conflict`，**未知 origin 不增加独立来源票数**（两个 unresolved 凑不成两个来源，由 agents 一侧执行）。取值与分配规则见下一条。同一事件的跨媒体归并仍由 agents 负责，data 不做。
- **`origin` 的取值与用法（v1.2）**：`origin` 只有三类前缀：`release:`（经确认的发布 ID `release:<命名空间>:<ID>`，或完整正文指纹确认的同稿 `release:fp:<指纹版本>:<指纹前缀>`）、`pub:<出版方族>`（命中出版方登记表的 (`provider`、`publisher`、`content_state`) 组合）、`issuer:cik:<10 位 CIK>`（SEC 申报本身；accession 不是 origin）。每条记录同时给 `origin_status`（`resolved` / `unresolved` / `conflict`）；`unresolved` 和 `conflict` 时 `origin` 为 `null`。**agent 只能用 `origin_status` 和两个 `origin` 是否相等做判断，不得解析前缀**；未知的 `origin_status` 取值按“不可用作独立来源”处理。分配按优先级（发布 ID → 正文指纹 → 出版方登记表 → 发行人 CIK）取第一个成立的，不跨优先级混合；同一优先级出现两个不同取值记 `conflict`。分量合并时追加 origin 等价记录（只在同一优先级内合并，规范值取字典序较小者，带发现时刻和关系 ID）：observed 只应用关系已可见、且等价本身在读取时点前已发现的记录；published 只应用关系在固定快照里的记录，与快照里的关系集合同进同出。
- **时间**：比较前先按 §2.1 更正各来源的时间戳（例如 Finnhub 的 Benzinga 偏早 4 小时）；不得用放宽时间窗口的方式掩盖时间错误。
- SEC 8-K 申报和 EX-99.1 新闻稿保留各自身份与时间；只有核实为同一份新闻稿的转载/附件才建立 same_release，不能把整个申报自动视为新闻稿。申报时间仍是 EDGAR 接收时间。
- **验证（将来做，现在没有结果）**：接入归因之前，须由人工核对至少 50 对候选与对照样本，覆盖建立链接的、拒绝链接的、标题相似但不同原文的情况。预先固定抽样与标注规则，逐规则报告 TP/FP/FN、样本召回与误合并；仅检查已链接对只能估精度，不能冒充总体召回率。尚无标注结果，方法与结果另交，不引用虚构数字。

## 5. 缺口信息

**第一版只服务已备好范围（v1.1）**：新增 `ready_range()`，返回 (ticker, session_date) 清单，带契约版本与完整性依据（要求的六类是否全部 `complete`）。`pairs` 只收要求的类别全部 `complete` 的对，其余放进 `incomplete_pairs` 并逐类给状态；要求了却没有声明需求的类别记 `NOT_DECLARED`，算不完整。`snapshot_id` 使用 `rr1:` 前缀，由 manifest/store SHA、日更任务库的 store_id、覆盖需求与计划哈希、要求的类别、截止时刻（UTC，微秒）和契约版本派生；调用方传入时必须与派生值一致。只按要求的类别准入：声明了但不在要求里的类别照常评估，不影响准入。邀请链接的日期选择器和可选列表都只用 `pairs`。日更另行生成轻量 `ready_range.json` 快照：其中 `pairs` 是日更已核验且有 movers 的 `(ticker, session_date)`，不做六类覆盖检查；P-7 的可选股票和日期列表从快照读取。`ready_range()` 则执行更严格的六类完整性检查，接入数据读取时再使用。下表的 `progress`、`deferred`、`breaker` 等补数据字段第一版 agents / app 不使用，保留不删。

**SEC 请求失败必须可见（v1.1）**：`sec_filings` 读取在索引请求失败时返回 `ERROR`（附原因码和 `attempted_at`），绝不返回空结果冒充 `EMPTY`；缓存命中标出缓存的取得时间。

**对外分类固定为六类**，不向前端暴露 provider 名称作为类别：

| `category` | 范围 |
|---|---|
| `price_bars` | 目标股、同行、板块 ETF 与大盘行情；用 scope 区分对象 |
| `sec_filings` | SEC 索引、申报与附件 |
| `target_news` | 当前目标股新闻，包含按目标股任务获取的 Alpaca/Benzinga 全文 |
| `peer_news` | 本次运行指定同行的新闻，包含其 Alpaca/Benzinga 全文 |
| `web_search` | 该运行所需的联网搜索及缓存覆盖 |
| `supplementary_news` | 其余补充新闻，例如 Kitco、行业/商品补充线索；取代旧名 benzinga_kitco，不代表 Benzinga 一律属于补充项 |

data 内部仍按来源与会话保存细分记录，再投影到上述类别（data-only 先交付；此处不实现 app/SSE）：

| 字段 | 说明 |
|---|---|
| `session_date`、`ticker`、`source_id`、`category`、`scope` | source_id 是登记表身份；scope 固定目标/同行/参照及对应主体，不能凭 provider 推断 target/peer |
| `status` | `OK` · `EMPTY` · `ERROR` · `NOT_FETCHED`（未抓）；OK/EMPTY 只说明请求结果，不自动证明窗口完整覆盖 |
| `progress` | 补数据的排队/运行/重试/完成状态，逻辑任务 ID、待补目标数与已完成数，最后更新时间 |
| `reason` | `ERROR`（仅错误类别）、`DISABLED`（缺 key 或登记为停用）、`deferred`（主机暂时不可用）、`breaker`（熔断）；明确记录本次策略不要求的项，不能冒充 EMPTY |
| `coverage_until` | 成功完成的查询窗口/游标覆盖到何时；不能用最新文章时间代替覆盖水位。与窗口起点、分页完成状态一起记录 |
| `mapping_version` | 内部来源到外部类别的映射版本，以及本次 target/peer 上下文身份 |

对一个逻辑任务，`target_count` 是本任务的会话总数，`completed_count` 是该任务当前已完成的会话数快照；同一快照写到该任务的每条会话缺口行，消费者不能把多行的 `completed_count` 相加。失败会话不计入完成数。完成状态表示采集任务结束，不等同于 `coverage_*` 已证明完整覆盖。

**SEC 重开规则**：SEC 会话是否完成由该 SEC 来源的最新 attempt 决定，按 `attempted_at`、再按 `attempt_id` 排序；最新状态为 `ERROR` / `DISABLED` 时会重新进入待处理。此规则只用于 SEC 日更，不改变其他来源当前的完成判定。若后续查询发现一份申报经索引页时间应归入此前已完成的会话，保留旧 attempt，并追加新 attempt；附件失败时，官方时间会话与尚未解决的 JSON 候选会话都记为 `ERROR` / `retrying`，下次仍须重试。重开使用新的逻辑任务 ID，避免把终态 `completed` 改回 `retrying`。正文、附件结构、intake attempt 和缺口行在同一 SQLite 事务提交，写入失败时整体回滚。

**内部映射实现**：版本化的 provider/source_id → category 映射位于 `data/manifests/source_categories_v1.json`，由 `m9/source_categories.py` 校验其与来源登记表一致。采集事实写入 `source_coverage_attempts`、`source_coverage_facts`、`source_coverage_targets`，只记录来源、产品、实际主体、查询范围及尝试，不写请求的 target/peer 角色。`m9.coverage_readiness.evaluate_coverage()` 按每次请求的显式需求计算完整性，`m9.gap_readiness.evaluate_snapshots(requirements, plan, verified_stores)` 校验跨库输入并投影成公开类别。`category_gaps` 和 `GapLedger` 保留旧账本兼容用途，不作为新接口的完整性依据。Alpaca 新闻的新覆盖写入已在本地实现；其余来源接入状态以 completion_status.md 为准。生产部署与正式消费者接入仍未完成。

类别聚合以调用方声明的所需任务与完整覆盖为准：只要缺少必需子任务、分页未完或所需范围未覆盖，就不能用另一 provider 的某条 OK 掩盖缺口。批准的补位仅在同一对象/范围被实际完整覆盖时消除缺口。原始来源记账、角色和证据资格保留，不因类别聚合改变。data 按下文规则计算完整性；app/agent 如何生成每次请求的需求项及其传输 DTO，仍由各自 session 对齐，此处不改这些包。

**需求输入与完整性判定（data-only）**：调用方显式声明唯一 `requirement_id`、类别、`session_date`、案例 ticker、scope、`subject_kind`/`subject_id`、`product`、请求窗口、语义过滤条件和内部 `source_ids`。ticker/instrument 主体匹配需求 scope_id，案例 ticker 不参与采集事实匹配；feed/query 只匹配其明确身份，不从文章标签推断。`CoveragePlan` 显式列出当前采集分区、来源能力及固定 feed 绑定；需求不复制采集哈希或分页执行参数。来源必须经登记映射到本次类别和 scope。

`source_policy=all` 要求每个来源完整；`any` 仅用于明示替代来源，一个完整非空来源可以满足，判定完整 EMPTY 则须所有替代来源完整且为空。每个相关当前子分区须取最新 attempt 的最新状态，并且全部完成，实际窗口并集覆盖需求的 `(start,end]`；未完成子分区不能被重叠成功遮住。枚举完整需 `enumeration_complete=true`，分页/截断未知保持 null；RSS 一次快照不证明历史完整。行情还须实际查询证明的会话集合覆盖所需会话；正文还须逐个取得明确枚举、去重后的正文目标。标题、正文、行情和搜索产品分别判断，旧行缺 product 不能证明标题或正文完整。

公开 `results` 按 `(session_date,ticker,category)` 分组，保留独立 `products`，每个产品列出声明的 requirement、scope、请求窗口、四种公开状态和完整标记。只有该类别的全部产品与 scope 都满足，类别才 `complete=true`；缺失 requirement IDs 明确列出。内部 PENDING/DISABLED 投影为 NOT_FETCHED，DISABLED 保留 reason；OK/EMPTY 本身仍不代表完整。进度计数只针对本次声明的需求集合，不跨任务相加，也不据读取结果捏造调度器的排队/运行状态。未声明类别不自动生成。

跨库输入必须由 `storage.snapshots.load_verified_stores()` 核对 input manifest、路径、SHA 和环境后创建，读取前后再次验证并使用只读事务；审计 `data_snapshot_ids` 使用实际 SHA。相同 SHA 可对应不同逻辑 store_id；完全一致的历史可去重，冲突历史或重复 store_id 报错，不用各库本地序号捏造全局先后。

数据就绪（RG-02）的消费者接入由 agent/app session 完成；缺口如何影响作答仍由其现有策略决定，data 不越权改变发布路径。

## 6. 来源在归因中的角色（负责人决定，2026-10-23；定档见 10-29 试运行结论）

**证据来源的边界（负责人决定）**：新来源中只有经 Alpaca 获取的 Benzinga 全文可作证据。角色按具体来源与原始出处登记；来源的可靠性档位与证据角色分开记录。Alpaca 返回的其他出版方内容不会仅因 provider 是 Alpaca 而自动升级为证据：
- Business Wire：正文页返回 403，不绕过。RSS 元数据只作线索；经其他已批准渠道获得的内容按该渠道的规则单独判定，不给 Business Wire 新开正文证据通道；
- PR Newswire：只存元数据（RSS），按既定决定不做正文；
- Seeking Alpha：哪些部分可用与来源分级（B7）一起确定，结论出来前只作线索；
- Marketaux、NewsAPI、GDELT：只作线索。

所有试运行中的来源都进入统一的证据库，统一记账、时间（`timefields`）、去重（§4）、监控和备份。**可靠性档位**（核心 / 补充 / 只作线索 / 淘汰）与**证据角色**（可作证据 / 只作线索）是两个独立的维度，分别记录、分开判断，不因为量少而拒绝接入。

## 7. 发行人身份

- CIK 是发行人的主键。股票代码按时点映射：改名、换代码要有历史对照表（RQ-18）。
- 多类股份（BRK-A/B、GOOGL/GOOG、FOX/FOXA）：新闻和申报按 CIK 共享，行情按各自的代码。`.` 和 `-` 两种写法统一成 `-`（RQ-17）。

## 8. 验收

- 每条规则都要有测试：时间规则、按时点读取（包括 `first_seen_at` 的界限和版本）、去重、缺口表，以及 SEC 与 Finnhub 两类已知错误的更正。
- 拿开发集的一个会话做回放：用契约读出的证据集合，与现有读取方式比较，差异要逐条说明。
- 契约版本写进每次运行的清单。

## 9. 显式未决项

以下是尚缺的事实、人工核验或下游接口信息，不改变 §1–8 已明确的实现规则；不得用猜测填充：

- **legacy 实际队列**：纳入规则已定。逐库确认路径、快照 SHA-256、记录身份、旧 `eligible_at` 规则和备份恢复情况之前，不打 `legacy` 标签；未知的正文损失数量保持未知。
- **时间更正表接入**：已新增按 SHA-256 校验 CSV、按精确 store scope 查询且不改写库的纯决策模块；它尚未接入 `as_of`。在接入完成前，不能声称 Finnhub/SEC 更正规则已端到端生效，也不能在更正前用原始时间作截止比较。
- **versioned 且缺少 `provider_updated_at`**：published 模式下不得作为时点已核验证据。回补内容标 `point_in_time_unverified` 并只作线索；非回补记录也不得作为已核验证据，保留“修改时间未知”原因。更细的下游展示文案留待消费者联调。
- **来源可靠性等级**：核心/补充/只作线索/淘汰按试运行数据定档，目标 10-29 PT；证据资格与可靠性分开，不改变 §6 已确认的证据角色。
- **去重质量结论**：至少 50 对人工标注尚未完成。标注前不报告召回率、误合并率或整体准确率，不把相似标题候选升级为已确认关联。
- **origin 稳定分配算法**（v1.1 新增）：取值与用法已在 §4 定下（v1.2）；实现在 `dedup/origin_assign.py`，接入 agent 前仍须人工核对样本，接入属于改变 agent 读取内容的变更，先报负责人。
- - **消费者传输 DTO 与生产接入**：本契约定义 data-only 字段与读取语义，不定义 app/SSE 的具体传输形状。评测结束后的 app/agent/eval 接入和生产切换另按负责人确认流程执行。

## 10. 版本记录

每条写清：改了什么、是否兼容（旧消费者不改代码是否仍然正确）、影响谁。不兼容的改动先交 boss，不直接并入。

| 版本 | 日期 | 改动 | 兼容 | 影响 |
|---|---|---|---|---|
| v1.0 | 2026-10-30 | 定稿。内容 = 原草案 v0.3，无字面改动 | — | 全部消费者以此为基线 |
| v1.1 | 2026-10-31 | 并入 CC2 审核意见 14 条中兼容的 13 条（#2 见下一行）：读取信封与三个哈希（#1，§3.0）；时点确认状态闭集与原因码（#3，§2）；`record_key`、`content_hash`（#5）；`content_state`、`usage_ceiling`、`usage_reason`（#6）；`url_path`/`provider_date` 只作线索（#7）；关系记录与 origin 未知策略（#8）；更正前后 `eligible_at` 都保留（#9）；夏令时夹具（#10）；SEC 请求失败必须 `ERROR`（#11）；缺旧正文重放失败（#12）；`ready_range()` 与补数据字段标注（#13）；`migration_manifest_sha256`（#14）；observed 下未知或晚于截止的版本连线索也不返回（#4，文字澄清，现有实现已是如此） | 兼容：全部为新增字段或文字澄清；未知枚举值按“不可用作证据” | 全部消费者；CC1 的读取接口需补字段 |
| v1.1 | 2026-10-31 | **#2 published 的截止点改为阶段 `window_end`**：`at` 省略取 `window_end`，`at` 晚于它抛错 | **不兼容**（v1.1 唯一一条）：此前 published 按 `at` 判定，`at` 晚于 `window_end` 的调用现在报错；`window_end` 之后被改写的正文不再作证据。目前没有使用方 | `temporal_evidence.as_of`、`as_of_stores`、`read_api.as_of`；负责人 10-31 采纳 |
| v1.2 | 2026-11-01 | `origin` 前缀限定为 `release:`、`pub:`、`issuer:cik:` 三类，`unresolved`/`conflict` 时为 `null`，agent 只用 `origin_status` 和 origin 是否相等，不解析前缀（§4，boss 11-01 决定） | 兼容：新增字段的取值约束；旧消费者不读 origin；目前没有读 origin 的使用方 | agents（独立来源计数）；`dedup/origin_assign.py` |
