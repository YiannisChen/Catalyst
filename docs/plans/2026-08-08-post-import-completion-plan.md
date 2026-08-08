# Post-Import Completion Plan（B6 完成路径）

- 日期：2026-08-08
- 分支 / code_revision 绑定：`recovery/b2o-data-readiness`，`B6_CODE_REVISION = bb43ebe20f29a13ef426e0a1a7c3aefc6d15ffd8`
- 状态：`INDEX_READY_B6_INCOMPLETE`

## Executive Summary

B6-G GPU embedding 与 LanceDB 导入已在本地完成并验证：`gpu_run_report.json` status=`completed`，
`import_report.json` status=`committed` / `ok=true` / `chunk_count=295506`，
active 指针指向表 `chunks__staging__b3761f4b943542a8`，dense smoke 命中 ≥1。
但 B6 仍未完成：runbook §10 要求的四臂产物、union 判定池、人工判定、B6 分析报告均未产出；
默认应用/agent 运行时也尚未切换到该 `lancedb_gold` 路径。本计划把“索引就绪”到“B6 契约完成 +
用户流程 smoke + 可选效果报告”拆成 A–F 六层、18 个任务（T13 拆 T13a/T13b）、5 个依赖波次。
本计划不宣称 B6 完成，也不宣称任何归因准确度数字。

## Revision: APPROVED_WITH_AMENDMENTS (2026-08-08)

主管审查结论 `APPROVED_WITH_AMENDMENTS` 后的修订清单：

- 新增 §4.1 Evidence contract：规范证据目录、`meta.json` 必填字段、每 Wave 落盘 `WAVE_TOKEN.txt` 纪律；T5/T7/T8/T9/T11 的 Evidence 引用该 layout。
- T4 增加 fail-closed 门禁：smoke pack 每 case 在 production served corpus 下 ticker + cutoff 过滤后 ≥1 条可检索 chunk，N/N 通过才可进 T5。
- T5 写死唯一 runner 入口 `packages/eval/scripts/run_post_import_four_arm.py`；禁止生产 dense/hybrid 使用 `run_frozen_eval.py` 的 hash/确定性 `_rag_query_embedding`（或等价 mock）而不标注。
- U1 示例命令标注 `depends: INDEX_WIRING_OK`；wiring 完成前该命令不得作为 pass 证据；保留 `run_frozen_eval.py` 示例时加粗警告假阴性。
- T13 拆为 T13a（人工判定协议 + 模板，可无真实标注）与 T13b（固定样本量 12 例的最小可复查标注）；§9 DoD-2 明确要求 T8 + T13a + T13b + T14。
- §8 增加：磁盘下限检查、reranker 无 GPU 时 fail-closed 策略、Task→测试映射（Wave 完成以 `data/run_reports/post_import/<run_id>/` 落盘为准）。
- 增加 GPU host 事实注记：`DEEPSEEK_API_KEY` 已写入 cpod-1trze1ad4jgy 的 `.env`×3（600 权限、git-ignored）；数据侧（lancedb_gold / 冻结 DB / snapshot pointer）尚未同步，跑通检索+归因前必须先同步；本计划任何位置不回显 key。

## Revision: MANAGER_PLAN_COMPLETE (2026-08-08)

主管就地补全（P2 收口，准许 Wave 1 执行）：

- T4 探针改为优先 `corpus_served_chunks` + current `manifest_id`（295506 行 served 视图）；`build_id` 仅作可选交叉校验，实施时先 `SELECT` 确认再写死。
- §8 provider 纪律去掉 key 前缀字面量；只允许「已配置 / len / 权限 / git-ignored」。
- T9 依赖改为 `T2, T3`（推荐 `T8` 但不强制）；mock graph 可与 pool 并行。
- 新增 §4.2 Wave token 机器可读约定；§7 补充 FTS5/SQLite 词法索引路径与 Mac vs GPU 执行面。
- 状态：本计划可作为执行真源；下一 session **Implement T1–T3 only**。

---

## §1 当前完成度地图

| 能力 | 状态 | 路径 / 证据 |
| --- | --- | --- |
| 数据就绪（B2-O/B3 冻结） | Done | 快照 `7a004acc…`；DB `data/snapshots/catalyst_b2o_7a004acc….db`；db_sha `bb37b213…`；source bundle `data/source_bundles/source_8ffae891b4e1…`；chunk_count=295506 |
| GPU embedding | Done（产物已拉回） | `data/embeddings/b6g_8ffae891_bb43ebe/gpu_run_report.json` status=`completed`；模型 `BAAI/bge-m3` @ `5617a9f6…`；(295506, 1024) float32 L2；duration ~17.8 min (RTX 4090) |
| LanceDB 导入 | Done（本地） | `data/lancedb_gold/b6g_8ffae891b4e1/import_report.json` status=`committed`；`active_generation.json` 表 `chunks__staging__b3761f4b943542a8`，chunk_count=295506；index_manifest_id `c7f4248b…`；clean import artifact `data/embeddings/b6g_import_bb43ebe/index_manifest.json` artifact_state=`lancedb_imported` |
| 运行时接线 | Partial | `RuntimeDependencyLoader` 默认 `lancedb_table_name="chunks"`（`packages/agents/catalyst_agents/runtime/dependencies.py`），未指向 active 表；`CATALYST_LANCEDB_DIR` 等 env 契约存在但未对生产 gold 路径接线 |
| 四臂评估（fts5/dense/hybrid/reranked） | Not started | `catalyst_data/retrieval/artifacts.py` / `hybrid.py` 已实现；尚无在本 production index 上产出的四臂 artifact；`run_frozen_eval.py` 默认指向 `data/lancedb_gold/eval_frozen` 且用硬编码 `_TABLE_NAME` |
| 归因图 / API 用户流 | Partial（代码） | `catalyst_agents.graph.build_attribution_graph`、AssuranceRecord、trace exporter 已实现并有测试；未用本 index + 真实 BGE-M3 查询 embedding 做过用户级 smoke；GPU host 已有 `DEEPSEEK_API_KEY`（真实 LLM 可选用） |
| 人工判定 / 准确度 | Not started | 无 union 判定池、无人工判定、无准确度指标（禁止宣称） |

## §2 目标与非目标

**目标**
- 完成 B6 剩余契约（runbook §10 五项中的后四项 + 导入后的接线确认）。
- 让应用/agent 默认运行时打开 active 表（不是 `"chunks"`）。
- 完成四臂产物、union 判定池、人工判定协议与最小标注、B6 分析报告。
- 完成诚实的用户流程 smoke（归因图 + API/workbench），产出可复现证据。
- 可选：B7 release hygiene（文档、CI、打包），不阻塞 B6 判定。

**非目标**
- 不重新 embedding、不重新 import（除非 manager 明确决定）。
- 不做 provider ingestion / 数据抓取 / 前端美化。
- 不宣称准确度——没有 T13a/T13b/T14 证据前不做任何准确度声明。
- 不 force-push main；merge/tag 遵循 T18 策略。

## §3 分层完成策略

| Layer | 名称 | 目的 |
| --- | --- | --- |
| A | Index contract | 数据/embedding/import 已就绪；只做接线确认（T1–T3） |
| B | Retrieval E2E（四臂 + pool） | 第一个“数据之后的完整流程”（T4–T8） |
| C | Attribution graph smoke | 模拟 analyst/agent 用户（T9–T10） |
| D | API / workbench smoke | 模拟普通产品用户（T11–T12） |
| E | Judgment + effect report | 组合/科学叙事（T13a/T13b–T15） |
| F | B7 / docs / release hygiene | E 之后或与 D 并行（T16–T18） |

**为什么 B 在 C/D 之前**：必须先固定检索身份（同一 revision + 同一 index + 同一 case pack 产出四臂），
否则 attribution/API 层会混合“检索问题”与“LLM 问题”，LLM 花费无法定位。先让检索身份可复现，再进入任何 LLM 流程。

---

## §4 任务分解（T1–T18，含 T13a/T13b）

> 每个任务都含：ID / Title / Layer / Priority / Depends on / Inputs / Work / Commands / Exit criteria / Evidence / Risks / Out of scope。
> 命令默认在 `/Users/yiannischen/Projects/Catalyst` 下运行；`.venv/bin/python` 按实际虚拟环境路径替换。

### §4.1 Evidence contract（规范目录，先于任何 Wave 执行）

每个 Wave / run 的证据统一落在规范目录，禁止只靠聊天记录或散落日志宣称完成：

```text
data/run_reports/post_import/<run_id>/
  meta.json              # 必填字段见下
  arms/<case_id>.json    # 四臂 artifact
  pool/<case_id>.json    # union pool（若适用）
  traces/                # 可选：graph/API traces
  WAVE_TOKEN.txt         # 单行 token，如 FOUR_ARM_E2E_OK
```

`meta.json` 必填字段（写死列表，缺一不可）：

| 字段 | 说明 |
| --- | --- |
| `code_revision` | 必须等于 `bb43ebe20f29a13ef426e0a1a7c3aefc6d15ffd8` |
| `index_manifest_id` | `c7f4248b2b70009a1d8c57d21075342dfe82e3e8417388a62667f9ba87bda083` |
| `corpus_manifest_id` | `3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc` |
| `snapshot_id` | `7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49` |
| `source_bundle_id` | `8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2` |
| `probe_report_id` | `25a596e98e4ff5e38f69f64f55404ddb05ab109c2c47d41f615c8744ec19ec23` |
| `postbuild_readiness_id` | `9f6c62fb0b077b1e946b4c6ee6779a6866b3c835b18acf213a045b75d91c472b` |
| `lancedb_dir` | `data/lancedb_gold/b6g_8ffae891b4e1` |
| `active_table_name` | `chunks__staging__b3761f4b943542a8` |
| `case_pack_id` / `case_pack_path` | 本 run 使用的 case pack 标识与路径 |
| `embedding_mode` | `production_pinned` \| `mock_unit_test` \| `degraded`（显式标注） |
| `git_head` | 应等于 `code_revision`；若不等必须记录差异原因 |
| `started_at` / `completed_at` | UTC ISO-8601 |

纪律：每一 Wave 退出时必须落盘 `WAVE_TOKEN.txt`（单行 token，如 `INDEX_WIRING_OK` / `FOUR_ARM_E2E_OK`）并更新 `meta.json`，作为后续 amendment 的对照证据；禁止只靠聊天记录宣称 wave 完成。

### §4.2 Wave token 机器可读约定

| Token | Wave | 最小 evidence_glob |
| --- | --- | --- |
| `INDEX_WIRING_OK` | 1 | `meta.json`, `WAVE_TOKEN.txt`, optional `traces/health.json` |
| `FOUR_ARM_E2E_OK` | 2 | `meta.json`, `WAVE_TOKEN.txt`, `arms/*.json`, `pool/*.json` |
| `USER_SMOKE_OK` | 3 | `meta.json`, `WAVE_TOKEN.txt`, `traces/*` |
| `JUDGMENT_EFFECT_REPORT_OK` | 4 | `meta.json`, `WAVE_TOKEN.txt`, judgments + report skeleton |
| `RELEASE_HYGIENE_OK` | 5 | runbook/README diffs referenced in `meta.json` |

`WAVE_TOKEN.txt` 内容为**单行** token 字符串（无前后空白以外的字符）。`meta.json` 的 `completed_at` 必须在写 token 时更新。

### T1 — 接线并文档化本 index 的运行时 env 契约
- Layer: A | Priority: P0 | Depends on: —
- Inputs: `data/lancedb_gold/b6g_8ffae891b4e1/active_generation.json`；`packages/agents/catalyst_agents/runtime/dependencies.py`；clean import artifact `data/embeddings/b6g_import_bb43ebe/index_manifest.json`
- Work:
  1. 确认 `RuntimeDependencyLoader` 读取的 env：`CATALYST_LANCEDB_DIR`、`CATALYST_CORPUS_MANIFEST_ID`、`CATALYST_INDEX_MANIFEST_ID`、`CATALYST_SOURCE_BUNDLE_ID`、`CATALYST_SNAPSHOT_ID`、`CATALYST_PROBE_REPORT_ID`、`CATALYST_POSTBUILD_READINESS_ID`。
  2. 确认 table 名来源：当前 loader 用构造参数 `lancedb_table_name`（默认 `"chunks"`），没有 env；本任务定义并实现“从 `active_generation.json` 读取 `table_name`”的契约（env 或 active-pointer 读取，改动最小）。
  3. 写一份运行时 env 契约段（可放本计划的附录或 runbook addendum，T16 再落 runbook）。
- Commands（验证当前默认值）:
  ```bash
  python3 - <<'PY'
  import json
  a = json.load(open('data/lancedb_gold/b6g_8ffae891b4e1/active_generation.json'))
  print(a['table_name'], a['chunk_count'], a['index_manifest_id'])
  PY
  ```
- Exit criteria: 从 `active_generation.json` 读取的 table 名被运行时使用；`RuntimeDependencyLoader(..., lancedb_table_name=<active>)` 成功 open 该表；文档记录全部 env 名与身份 id。
- Evidence: 修改后的 `dependencies.py`（如改）、env 契约文档、`data/run_reports/post_import/<run_id>/meta.json`（§4.1）与 `WAVE_TOKEN.txt`（Wave 1 出口）。
- Risks: 若修改 loader 默认值，必须保证现有测试（`test_runtime_assembly.py`、`test_runtime_dependencies.py`）仍通过；不要为满足本任务改动契约 schema。
- Out of scope: 不改变 `active_generation.json` schema；不 re-embed。

### T2 — Health/assembly 检查：打开 active 表（非默认 `"chunks"`）
- Layer: A | Priority: P0 | Depends on: T1
- Inputs: `data/lancedb_gold/b6g_8ffae891b4e1/`；app/agents health 组装代码（`packages/app/tests/test_runtime_assembly.py`、`packages/agents/catalyst_agents/runtime/dependencies.py`）
- Work:
  1. 用 `CATALYST_LANCEDB_DIR=data/lancedb_gold/b6g_8ffae891b4e1` + active table 名启动 app/agents 运行时，检查 health。
  2. 断言 health 中 `lancedb` / `retrieval` 状态 ready，且表名 = `chunks__staging__b3761f4b943542a8`，index_manifest_id = `c7f4248b…`。
  3. 特别注意 `run_frozen_eval.py` 的 `_open_lancedb_table` 用硬编码 `_TABLE_NAME` —— 确认生产路径不经过它，或在本任务内修正为 active pointer 读取。
- Commands（参考）:
  ```bash
  CATALYST_LANCEDB_DIR=data/lancedb_gold/b6g_8ffae891b4e1 \
  CATALYST_CORPUS_MANIFEST_ID=3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc \
  CATALYST_INDEX_MANIFEST_ID=c7f4248b2b70009a1d8c57d21075342dfe82e3e8417388a62667f9ba87bda083 \
  .venv/bin/python -m pytest packages/agents/tests/test_runtime_dependencies.py -q
  ```
- Exit criteria: health 显示 active 表被打开；一条指向 active 表的 `count_rows=295506` 可复现；没有任何路径回退到默认 `"chunks"` 而不告警。
- Evidence: health JSON 日志、运行中的测试输出，写入 §4.1 `data/run_reports/post_import/<run_id>/`。
- Risks: 若某 CLI/runner 仍硬编码 `"chunks"`，会产生“index 就绪但检索空结果”的假阴性/假阳性；必须 fail-closed 而不是静默回退。
- Out of scope: 不改 LanceDB 表内数据。

### T3 — Query embedding 路径策略（固定 BGE-M3 revision；离线/缓存/mock 边界）
- Layer: A | Priority: P0 | Depends on: T1
- Inputs: `packages/agents/catalyst_agents/runtime/query_embedding.py`（`ProductionBgeM3QueryEmbeddingFactory`：pinned `BGE_M3_REVISION`，CUDA 必需，无 CPU 回退）
- Work:
  1. 固化生产查询 embedding 策略：真实查询必须走 pinned `BGE-M3` @ `5617a9f61b028005a4858fdac845db406aefb181`，且与索引向量同 revision/dim/normalization。
  2. 明确离线/本地缓存策略：本地无 CUDA 时禁止静默 CPU 回退；`run_frozen_eval.py` 里的确定性 hash `_rag_query_embedding` 只允许用于 frozen/mock/单测，不得用于生产 dense arm。
  3. 给单元测试的 mock embedding 定义显式边界（允许 mock，但 artifact 必须标注 embedding 模式）。
- Commands: 无新增命令；运行 `packages/agents/tests/test_query_embedding_factory.py` 确认现有契约。
- Exit criteria: 文档写明三种模式（production pinned / offline local cache / mock-for-unit-test）及允许场景；生产 dense 路径强制 revision 校验。
- Evidence: 策略文档段；`test_query_embedding_factory.py` 通过；Wave 1 `meta.json`（§4.1）中 `embedding_mode` 按实际写。
- Risks: 若查询 embedding 与索引向量 revision 不一致，dense 分数无意义；CUDA 缺失时若出现回退会污染结果。
- Out of scope: 不下载新权重；不做 CPU fallback。

### T4 — Case pack 选择（复用 pre-B6 词法基线 + 3–12 case smoke pack）
- Layer: B | Priority: P0 | Depends on: T3
- Inputs: `packages/eval/golden_set/`（`pre_b6_attribution_cases_v1.jsonl`、`v1_2_p0_set.jsonl`、`v1_2_p1_set.jsonl`、`v1_3_answerable.jsonl`、`v1_3_unanswerable.jsonl`、`h_refusal_cases.validated.json`）；`packages/eval/scripts/freeze_pre_b6_lexical_baseline.py`（12-case 基线）
- Work:
  1. 选定 3–12 个 case 的 smoke pack：优先复用 pre-B6 词法基线 12 例的子集；记录每个 case 的 id、ticker、session_date、cutoff、golden evidence（若存在）。
  2. 写明加载方式（JSONL 字段契约），并确认这些 case 在本 production DB（`data/snapshots/catalyst_b2o_7a004acc….db`）有对应 chunk。
- Commands（列出 golden set）:
  ```bash
  python3 - <<'PY'
  import json
  for p in ['packages/eval/golden_set/pre_b6_attribution_cases_v1.jsonl',
            'packages/eval/golden_set/v1_2_p0_set.jsonl']:
      with open(p) as f:
          rows = [json.loads(l) for l in f if l.strip()]
      print(p, len(rows), sorted(rows[0].keys()))
  PY
  ```
- **Exit criteria（fail-closed 门禁）**:
  1. case pack 文件（如 `data/run_reports/post_import_smoke_cases.jsonl`）含 3–12 例，字段完整，全部可被检索层消费。
  2. 每个 case 在 production **served corpus** 下，其 ticker + cutoff 过滤后至少 1 条可检索 chunk；退出条件写成 **N/N cases pass existence probe**，任一 case 失败即 fail-closed，**不得进入 T5**。
  3. 探针命令骨架（只读 sqlite；**优先** `corpus_served_chunks` + current `manifest_id`，不要硬猜 `build_id`）：
  ```python
  import sqlite3
  from pathlib import Path
  DB = "data/snapshots/catalyst_b2o_7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49.db"
  MANIFEST_ID = "3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc"
  conn = sqlite3.connect(f"file:{Path(DB).resolve().as_uri()}?mode=ro&immutable=1", uri=True)
  # optional cross-check only (do not hardcode without SELECT first):
  # builds = conn.execute(
  #   "SELECT build_id, COUNT(*) FROM corpus_build_chunks GROUP BY 1 ORDER BY 2 DESC"
  # ).fetchall()
  def probe(ticker: str, cutoff_iso: str) -> int:
      return conn.execute(
          """
          SELECT COUNT(*) FROM corpus_served_chunks c
          WHERE c.manifest_id = ?
            AND c.status = 'active'
            AND c.eligibility = 'eligible'
            AND c.available_at <= ?
            AND EXISTS (
              SELECT 1 FROM json_each(c.ticker_associations) je
              WHERE je.value = ?
            )
          """,
          (MANIFEST_ID, cutoff_iso, ticker),
      ).fetchone()[0]
  # for each smoke-pack case: assert probe(ticker, cutoff) >= 1
  # exit when N/N pass; else fail-closed (no T5)
  ```
- Evidence: case pack 文件；存在性探针结果（N/N 记录）；加载测试；可附 `SELECT COUNT(*) FROM corpus_served_chunks` == 295506 旁证。
- Risks: case 若引用旧 snapshot 的 chunk，检索会 miss——探针不过即失败，不进入 T5；`ticker_associations` 为 JSON 数组字符串（如 `["AAPL"]`），必须用 `json_each`。
- Out of scope: 不改 golden set 内容；不新增外部 case。

### T5 — 四臂 batch runner（fts5 / dense / hybrid / reranked，production index + frozen DB）
- Layer: B | Priority: P0 | Depends on: T2, T3, T4（且依赖 `INDEX_WIRING_OK`）
- Inputs: `data/lancedb_gold/b6g_8ffae891b4e1`（active 表）；frozen DB `data/snapshots/catalyst_b2o_7a004acc….db`；`catalyst_data.retrieval.hybrid.ProductionHybridRetriever`；`catalyst_data.retrieval.artifacts.write_arm_artifact`（arms 顺序 `["fts5","dense","hybrid","reranked"]`，root 默认 `data/retrieval_arm_outputs`）
- **唯一 runner 入口（写死）**：`packages/eval/scripts/run_post_import_four_arm.py`。若该文件尚不存在，由下一执行 session 实现；本计划以该路径为唯一入口契约，任何其他入口（含 `run_frozen_eval.py`）不得作为生产四臂产物来源。
- Work:
  1. 实现/确认 `run_post_import_four_arm.py`：对每个 case 跑四个 arm，调用 artifact writer 落 §4.1 `<run_id>/arms/<case_id>.json`，并写 `meta.json`。
  2. dense/hybrid arm 使用生产查询 embedding（T3 策略），rerank 使用 pinned reranker 模型（`BAAI/bge-reranker-v2-m3`，候选集保持、超时/失败回退 RRF，见 b6 plan Task 4/5）。
- **禁止**：生产 dense/hybrid 使用 `run_frozen_eval.py` 的 hash/确定性 `_rag_query_embedding`（或等价 mock）而不在 `meta.json` 标注 `embedding_mode=mock_unit_test`；`embedding_mode=production_pinned` 时禁止混入 hash 向量。
- Commands（1-case smoke 模板；依赖 `INDEX_WIRING_OK`）:
  ```bash
  .venv/bin/python packages/eval/scripts/run_post_import_four_arm.py \
    --db data/snapshots/catalyst_b2o_7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49.db \
    --lancedb-dir data/lancedb_gold/b6g_8ffae891b4e1 \
    --active-table chunks__staging__b3761f4b943542a8 \
    --case-pack data/run_reports/post_import_smoke_cases.jsonl \
    --run-id smoke_1 \
    --limit 1 \
    --embedding-mode production_pinned \
    --output-root data/run_reports/post_import
  ```
  （具体 CLI 以该脚本实现为准；本模板是契约骨架。）
- Exit criteria:
  1. 每 case 四臂 artifact 生成，schema_version=`1.0.0`，arms 顺序 `["fts5","dense","hybrid","reranked"]`，artifact_id 与语义字段稳定（时间/latency 不参与 id）。
  2. `embedding_mode=production_pinned` 时 query embedding revision == 索引 BGE-M3 revision `5617a9f61b028005a4858fdac845db406aefb181`。
  3. `meta.json` 必填字段完整（§4.1）。
- Evidence: `data/run_reports/post_import/<run_id>/arms/*.json` + `meta.json`（§4.1）。
- Risks: 四臂 runner 若复用 frozen eval 的 hash embedding 会产出无效 dense 分数；禁止混用。
- Out of scope: 不在此任务做准确度评估；不调 LLM。

### T6 — Cutoff / ticker / filter 回归（production 表上）
- Layer: B | Priority: P1 | Depends on: T5
- Inputs: B4 cutoff-safe lexical 契约（`docs/plans/2026-07-22-b4-cutoff-safe-lexical-retrieval.md`）；production 表；case pack
- Work: 对每个 case 断言：cutoff 生效（无未来 chunk 泄露）、ticker 过滤正确、非法 cutoff 被拒绝。
- Exit criteria: 全部 case 的 cutoff/ticker 过滤回归通过；无 look-ahead 命中。
- Evidence: 回归报告（JSON/日志），随 §4.1 `<run_id>/` 归档。
- Risks: cutoff 时区/边界处理错误会静默漏泄；用冻结 DB 的已知 chunk 时间做锚点。
- Out of scope: 不修改 B4 契约。

### T7 — 持久化 arm artifacts 并绑定身份
- Layer: B | Priority: P0 | Depends on: T5
- Inputs: §4.1 `<run_id>/arms/`；clean import artifact 身份（index_manifest_id、corpus_manifest_id、code_revision）
- Work: 在 `meta.json` 中记录 `code_revision=bb43ebe…`、index_manifest_id、corpus_manifest_id、snapshot_id、case pack id、embedding 模式；校验与 §7 cheat sheet 一致。
- Exit criteria: 每个 run 的 `meta.json` 字段完整；artifact_id 校验通过。
- Evidence: §4.1 `data/run_reports/post_import/<run_id>/meta.json`；`checksums` 可选。
- Risks: 身份缺失会让后续分析无法复现；写入前校验，不做“先跑后补”。
- Out of scope: 不 stage/commit（本任务只是落盘）。

### T8 — Union pool 生成（独立 oracle 纪律）
- Layer: B | Priority: P0 | Depends on: T7
- Inputs: `catalyst_data.retrieval.pool.py`；四臂 artifacts；`packages/data-core/tests/test_union_pool.py`（若有）
- Work:
  1. 对 smoke case 生成 union pool：合并四臂结果、去重（arm 内重复 chunk id 拒绝）、校验缺失 arm / 畸形 id / source-artifact 身份不匹配。
  2. 独立 oracle 纪律：测试期望值（union 顺序、per-arm chunk-id 元组、manifest id、source artifact id）必须是字面量，不得从生产 helper 反推（runbook §10 规定）。
- Exit criteria: pool 生成成功且校验全过；测试无 circular oracle。
- Evidence: §4.1 `data/run_reports/post_import/<run_id>/pool/<case_id>.json` + 校验日志。
- Risks: 若测试从 `per_arm_chunk_ids` 推导期望，等于自证，禁止。
- Out of scope: 不做 keep/kill 决策。

### T9 — 3-case 归因图 in-process smoke（先 mock LLM，可选真实 LLM flag）
- Layer: C | Priority: P0 | Depends on: **T2, T3**（推荐并行/先后完成 T8，**不强制**依赖 pool 才开 mock graph）
- Inputs: `catalyst_agents.graph.build_attribution_graph`；AssuranceRecord（`packages/agents`）；fixture/mock LLM（frozen eval 已有 `FrozenGraphLLM` 等）；GPU host `DEEPSEEK_API_KEY`（真实 LLM 可选用，见 §7）
- Work: 取 3 个 case，用 fixture/mock LLM 走完整图状态机（judge → critic → assurance），断言状态合法；提供可选 `--real-llm` flag 但默认关闭。生产检索路径须走 active 表（T2）；mock retriever 仅允许在 `meta.json` 标注 `embedding_mode=mock_unit_test` 的单元场景。
- Exit criteria: 3 例图状态机完成，无异常；mock 路径零网络。
- Evidence: §4.1 `data/run_reports/post_import/<run_id>/traces/` + assurance 记录。
- Risks: 真实 LLM 引入成本与不可复现性——默认 mock，真实运行必须显式 flag 且需 manager 批准；key 不回显；Mac 8Gi 内存不足时 **必须** 在 GPU host 跑（先同步 lancedb_gold + DB）。
- Out of scope: 不评估准确度。

### T10 — Assurance 记录 + ABSTAIN/SUFFICIENT 合法性检查
- Layer: C | Priority: P1 | Depends on: T9
- Inputs: B5 契约（`docs/plans/2026-07-22-b5-attribution-runtime-assurance.md`：公式、gate、ranking 定义）
- Work: 对每个 assurance 记录校验：输出状态 ∈ {SUFFICIENT, PARTIAL, ABSTAIN, INSUFFICIENT}，gate 条件合法，证据 id 均来自检索结果。
- Exit criteria: 全部记录合法；ABSTAIN 场景有覆盖（用 `h_refusal_cases.validated.json` 或同款 refusal case）。
- Evidence: assurance 校验报告，随 §4.1 `<run_id>/` 归档。
- Risks: 非法状态被静默接受会污染后续准确度叙事；fail-closed。
- Out of scope: 不改变 B5 schema。

### T11 — HTTP happy path（health → run → fetch result/trace）
- Layer: D | Priority: P0 | Depends on: T9, T1
- Inputs: app/API 层（`packages/app` 或等价 workbench/API 入口）；生产 runtime 接线（T1/T2）；GPU host `DEEPSEEK_API_KEY`（若走真实 LLM）
- Work: 用 smoke case 走真实 HTTP：`GET /health` → 提交 run（ticker + session_date + cutoff）→ 拉取 result + trace。
- Exit criteria: 全链路 200，result 与 trace 与 T9 的 in-process 输出一致（同一输入）。
- Evidence: §4.1 `data/run_reports/post_import/<run_id>/traces/` + API 响应日志。
- Risks: HTTP 层若用默认 `"chunks"` 表会返回空结果——先过 T2；key 不经日志。
- Out of scope: 不做前端 UI。

### T12 — Failure paths（坏 lance 路径 / manifest 不匹配 / 非法 cutoff 明确报错）
- Layer: D | Priority: P1 | Depends on: T11
- Work: 构造 3 类失败输入并断言返回显式结构化错误（非 500 泛化）。
- Exit criteria: 坏路径、manifest mismatch、非法 cutoff 均返回明确错误码/消息。
- Evidence: 失败场景测试日志，随 §4.1 `<run_id>/` 归档。
- Risks: 静默回退（如切到空表）是最高风险，必须 fail-closed。
- Out of scope: 不改契约。

### T13a — 人工判定协议 + 模板（不做真实标注）
- Layer: E | Priority: P0 | Depends on: T8
- Inputs: `packages/eval/golden_set/annotation_template.md`；B7-H Task 8 人类标注协议（`docs/plans/2026-07-22-b7-api-evaluation-release.md` §7 Task 8）
- Work: 定义 rubric（判定维度、ABSTAIN 规则）、存储路径（如 `data/eval_reports/judgments/`）、去偏差（独立于 runner 编写者）；生成可执行的标注模板。
- Exit criteria: 协议文档可执行，标注文件 schema 固定。
- Evidence: 协议文档 + 标注模板。
- Risks: 判定者与实现者同源会产生 circular 偏差；协议必须写明独立性。
- Out of scope: T13a 不做真实标注（标注在 T13b）。

### T13b — 最小可复查样本标注（固定样本量）
- Layer: E | Priority: P0 | Depends on: T13a
- Inputs: T13a 协议与模板；§4.1 pool 产物；union 判定池
- Work: 对**固定样本量 = 12 例**（从 smoke pack 全量 + 代表性扩展中选取；若 smoke pack 本身 ≥12 则直接用其全量 12 例）执行最小可复查标注；记录判定者、日期、每例判定结果与理由。
- Exit criteria: 12 例标注完成且可复查（rubric 合规）；标注文件 schema 与 T13a 一致。
- Evidence: `data/eval_reports/judgments/` 下的标注文件（含判定者与复查信息）。
- Risks: 样本太小不可外推准确度——只做最小可复查证据，不做大规模；不得据此宣称准确度。
- Out of scope: 不做大规模标注（大规模人工判定超出本计划）。

### T14 — 分析报告模板（arm 对比；禁止 overclaim）
- Layer: E | Priority: P0 | Depends on: T13a, T13b
- Inputs: 四臂 artifacts、pool、T13b 真实判定输入
- Work: 模板包含：arm 级指标（命中/顺序/覆盖率）、human judgment 汇总、ABSTAIN 率、限制声明；明确“无 T13a/T13b+T14 数据不得宣称准确度”。
- Exit criteria: 模板可生成报告骨架，并由 T13b 提供真实判定输入填充。
- Evidence: 模板文件 + 生成的报告（至少空骨架）。
- Risks: overclaim 会破坏组合可信度；模板内置“限制”小节。
- Out of scope: 不填充未实测的指标。

### T15 — Public narrative / README limitations 更新
- Layer: E | Priority: P2 | Depends on: T14
- Work: 更新 `README.md` 与相关 docs：B6 状态行、数据身份、限制（无准确度声明、本地 smoke 范围）。
- Exit criteria: 公开文档与事实一致，无“B6 complete”字样（除非真实完成全部 §9 DoD-2）。
- Evidence: diff。
- Risks: 叙事超前于证据；以状态 token 为准。
- Out of scope: 不做营销性夸大。

### T16 — Operator runbook addendum（post-import 本地阶段路径）
- Layer: F | Priority: P1 | Depends on: —
- Work: 在 `docs/plans/2026-08-06-b6-g-cloud-execution-runbook.md`（或独立 addendum）追加：本机使用的 `data/embeddings/b6g_8ffae891_bb43ebe/`（full）、`data/embeddings/b6g_import_bb43ebe/`（clean import）、`data/lancedb_gold/b6g_8ffae891b4e1/` 及其用途与安全删除条件。
- Exit criteria: runbook 可指导后续操作者区分 full/import/gold 目录。
- Evidence: runbook diff。
- Risks: 误删 1.2GB `vectors.npy` 或 shards 会导致无法重建；写明保留策略。
- Out of scope: 不删除任何数据。

### T17 — Import validator allowlist（`gpu_run_report.json` / `shards/`）
- Layer: F | Priority: P2 | Depends on: —
- Work: 检查 `packages/data-core/catalyst_data/retrieval/gpu_contract.py` 的 import validator 是否对 full artifact 目录（含 `gpu_run_report.json`、`shards/`）报“unexpected file”；若报，则按需将 clean-import 产物限定为 4 文件（现状）并文档化，或增加 allowlist。
- 注意：若改 validator 代码，新的 `code_revision` 会改变 embedding/import 身份绑定——需 manager 决定是否值得重跑身份校验（不重 embed，只重算 manifest）。
- Exit criteria: 文档说明 clean-import 目录 4 文件是预期状态；validator 行为与 `lancedb_imported` 一致。
- Evidence: validator 运行日志或文档段。
- Risks: 改代码影响 `code_revision` 身份；默认不改，只文档化。
- Out of scope: 不重跑 import。

### T18 — Branch/tag 策略（B6 之后何时 merge main；不 force）
- Layer: F | Priority: P1 | Depends on: Wave 2 完成（T8）
- Work: 与 manager 确认：B6 契约完成（§9 DoD-2）后，`recovery/b2o-data-readiness` 以普通 merge/PR 进 main；tag 命名与 `B6_CODE_REVISION` 绑定；禁止 force-push。
- Exit criteria: 策略写入 README/docs；merge 前跑完 P0 gate。
- Evidence: 策略文档。
- Risks: 未经 DoD-2 就 merge 会被误读为 B6 完成。
- Out of scope: 本任务不执行 merge。

---

## §5 全流程测试计划（模拟正常使用）

### U1 检索用户（query + ticker + cutoff）
- **depends: `INDEX_WIRING_OK`（T1–T3）**；wiring 完成前，下述命令不得作为 pass 证据引用。
- Given：生产 runtime 已接线（T1/T2），四臂 runner 可用（T5）
- When：用户提交 `(query, ticker, cutoff)`；Then：返回 fts5/dense/hybrid/reranked 四臂结果，cutoff 无泄漏
- Layers: A + B；最大 case 数：smoke ≤ 12，full 用全部基线 case
- Pass：每 arm 结果非空、cutoff 过滤正确、artifact 落盘 §4.1 `<run_id>/arms/`
- Fail：任一 arm 空/回退 `"chunks"`/embedding revision 不匹配
- 示例命令（唯一 runner 入口）:
  ```bash
  .venv/bin/python packages/eval/scripts/run_post_import_four_arm.py \
    --db data/snapshots/catalyst_b2o_7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49.db \
    --lancedb-dir data/lancedb_gold/b6g_8ffae891b4e1 \
    --active-table chunks__staging__b3761f4b943542a8 \
    --case-pack data/run_reports/post_import_smoke_cases.jsonl \
    --run-id smoke_1 \
    --embedding-mode production_pinned \
    --output-root data/run_reports/post_import
  ```
- 若仍用 `run_frozen_eval.py` 示例（**加粗警告**）：**该脚本 `_open_lancedb_table` 在未修正 active table 前会打开硬编码 `_TABLE_NAME`，必然假阴性；wiring 完成前禁止引用其输出作为 pass 证据。**
  ```bash
  .venv/bin/python packages/eval/scripts/run_frozen_eval.py \
    --db data/snapshots/catalyst_b2o_7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49.db \
    --lancedb-dir data/lancedb_gold/b6g_8ffae891b4e1 \
    --pipeline-mode rag_rerank \
    --golden-set data/run_reports/post_import_smoke_cases.jsonl
  ```

### U2 归因用户（ticker + session_date + cutoff → graph）
- Given：T9/T10 通过
- When：用户提交 `(ticker, session_date, cutoff)`；Then：返回归因图 + assurance 记录，状态合法
- Layers: C（+ A/B 数据）；最大 case 数：3（smoke）/ full 另定
- Pass：状态 ∈ {SUFFICIENT, PARTIAL, ABSTAIN, INSUFFICIENT}，证据 id 均来自检索结果
- Fail：状态非法、证据 id 越界、mock 路径出现网络调用
- 真实 LLM 变体：显式 `--real-llm` flag（GPU host 有 `DEEPSEEK_API_KEY`）；默认 mock。

### U3 操作员失败用户（misconfig）
- Given：T12 通过
- When：坏 `CATALYST_LANCEDB_DIR` / manifest 不匹配 / 非法 cutoff；Then：显式错误
- Layers: D；case 数：3 类失败 × 1
- Pass：结构化错误返回，无静默回退
- Fail：返回 200 空结果或泛化 500

### U4 可复现性（same revision + index + case）
- Given：T7/T8 身份绑定
- When：同一 `B6_CODE_REVISION` + 同一 active 表 + 同一 case pack 重跑；Then：artifact_id 与语义字段一致
- Layers: A + B；case 数：1–3（smoke）
- Pass：语义字段 hash 稳定（时间/latency 除外）
- Fail：identity 字段漂移

---

## §6 执行波次（日历无关，依赖驱动）

| Wave | Tasks | Entry criteria | Exit token |
| --- | --- | --- | --- |
| 1 | T1–T3 | 本计划已确认；git 树干净；§7 身份全部对上 | `INDEX_WIRING_OK` |
| 2 | T4–T8 | Wave 1 完成；active 表可被运行时打开；T4 N/N 探针通过 | `FOUR_ARM_E2E_OK` |
| 3 | T9–T12 | Wave 2 完成；四臂 + pool 存在 | `USER_SMOKE_OK` |
| 4 | T13a–T13b, T14–T15 | Wave 3 完成（或与 D 并行） | `JUDGMENT_EFFECT_REPORT_OK` |
| 5 | T16–T18 | Wave 2 完成（T18）；T16/T17 可提前 | `RELEASE_HYGIENE_OK` |

每 Wave 退出必须按 §4.1 落盘 `WAVE_TOKEN.txt` + 更新 `meta.json`。

---

## §7 环境与路径速查表

| 项 | 值 |
| --- | --- |
| 分支 / code_revision | `recovery/b2o-data-readiness` / `bb43ebe20f29a13ef426e0a1a7c3aefc6d15ffd8` |
| Active snapshot pointer | `data/manifests/active_data_snapshot.json` |
| Production DB | `data/snapshots/catalyst_b2o_7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49.db`（db_sha `bb37b213091e256033fa00272cb7a85617dbcddf69d6fe9b515840cd9f1ebe40`） |
| Served corpus build_id（corpus manifest `32740692…`） | `3839ca95828ccc95b25f18b4ec9b0600fdf5d5abb6fa8dcf903a806392fed51c`（`corpus_build_chunks`，status=`active`，295506 行） |
| Source bundle | `data/source_bundles/source_8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2` |
| Full embedding artifact | `data/embeddings/b6g_8ffae891_bb43ebe/`（含 `gpu_run_report.json`、`shards/`） |
| Clean import artifact | `data/embeddings/b6g_import_bb43ebe/`（4 文件：`checksums.sha256`、`chunk_ids.json`、`index_manifest.json`、`vectors.npy`；hardlinks） |
| LanceDB gold dir | `data/lancedb_gold/b6g_8ffae891b4e1/` |
| Active table | `chunks__staging__b3761f4b943542a8`（`active_generation.json`，chunk_count=295506） |
| index_manifest_id | `c7f4248b2b70009a1d8c57d21075342dfe82e3e8417388a62667f9ba87bda083` |
| corpus_manifest_id | `3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc` |
| snapshot_id | `7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49` |
| source_bundle_id | `8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2` |
| probe / postbuild | `25a596e98e4ff5e38f69f64f55404ddb05ab109c2c47d41f615c8744ec19ec23` / `9f6c62fb0b077b1e946b4c6ee6779a6866b3c835b18acf213a045b75d91c472b` |
| Embedding model | `BAAI/bge-m3` @ `5617a9f61b028005a4858fdac845db406aefb181`，(295506, 1024) float32 L2 |
| Reranker model | `BAAI/bge-reranker-v2-m3`（pinned per `lancedb_store.RERANKER_MODEL`） |
| Key env vars | `CATALYST_LANCEDB_DIR`、`CATALYST_CORPUS_MANIFEST_ID`、`CATALYST_INDEX_MANIFEST_ID`、`CATALYST_SOURCE_BUNDLE_ID`、`CATALYST_SNAPSHOT_ID`、`CATALYST_PROBE_REPORT_ID`、`CATALYST_POSTBUILD_READINESS_ID` |
| GPU host（归因/LLM + 重检索） | `cpod-1trze1ad4jgy`，仓库 `/workspace/catalyst/Catalyst`（HEAD=`bb43ebe…`）；`DEEPSEEK_API_KEY` 已写入 `.env`×3（600 权限，git-ignored，**不在文档回显**）；**数据侧未同步**：须 rsync `data/lancedb_gold/b6g_8ffae891b4e1/`、冻结 DB、`data/manifests/active_data_snapshot.json`（及按需 embeddings）并设置 `CATALYST_LANCEDB_DIR` 等，才能跑通检索+归因 |
| Lexical / FTS5 源 | 同一冻结 SQLite：`corpus_served_chunks` + FTS 侧表（如 `corpus_build_chunks_fts` / served 发布路径）；词法 arm 与 dense arm **共享** `corpus_manifest_id=32740692…` |
| Execution surface | **Mac**：T1–T3 wiring、只读探针、小 smoke；**GPU host**：生产四臂 dense/rerank、真实 query embedding、真实 LLM 归因（Mac 8Gi 不作为生产检索/归因主机） |
| Evidence root | `data/run_reports/post_import/<run_id>/`（§4.1） |

---

## §8 风险与 fail-closed 规则

1. **Dirty git 树**：production CLI（embed/import/校验）在脏树下运行会破坏身份绑定——任何产线操作前 `git status --porcelain` 必须为空。
2. **`code_revision` 必须与 embedding/import 身份一致**：一切新产物的 metadata 都写 `bb43ebe…`；一旦改动代码，需 manager 决定是否重算 manifest 身份。
3. **不 re-embed**：除非 manager 明确决定；现有 `(295506, 1024)` 向量是唯一事实源。
4. **本机磁盘压力与下限检查**：full artifact ~1.2GB `vectors.npy` + shards + LanceDB 表 + 四臂/pool 产物；每 Wave 开始前 `df -h .`，剩余 < 10Gi 时不得开始新 run（先按 T16 保留策略清理/外移）；保留策略按 T16 执行，不擅自删除。
5. **Import validator unexpected files**：`gpu_run_report.json`、`shards/` 存在于 full artifact，clean-import 4 文件才是 `lancedb_imported` 状态；按 T17 文档化，不改代码默认。
6. **Reranker/model 下载策略（写死：fail-closed）**：无 GPU 时 production 四臂 run 必须失败并给出明确错误，禁止静默降级（既不自动 skip rerank，也不静默只跑 hybrid）；任何降级路径必须 manager 显式批准，且 `meta.json` 标注 `embedding_mode=degraded` 与缺失 arm。超时/忙时 **RRF fallback** 仍属契约内降级，须写入 arm artifact 的 degradation 字段。新权重下载需 manager 批准。
7. **Provider key 纪律**：`DEEPSEEK_API_KEY` 已存在于 GPU host（`.env`×3，600 权限，git-ignored）；任何日志/计划/报告 **不得** 回显 key 或可识别前缀；校验只允许 `configured=true` + `len`（可选）。调用 provider 仅限 T9/T11 显式 `--real-llm` 且需 manager 批准。
8. **无准确度声明**：没有 T13a/T13b+T14 完成并校验前，任何文档/汇报不得宣称准确度指标或 B6 完成。
9. **Task→测试映射**：Wave 完成不能只靠既有 unit pytest 全绿；E2E 证据以 `data/run_reports/post_import/<run_id>/`（§4.1，`meta.json` + `WAVE_TOKEN.txt` + 产物）为准。Unit 测试是必要非充分条件。

---

## §9 Definition of Done

1. **Engineering demo ready**（Wave 1–3）：`INDEX_WIRING_OK` + `FOUR_ARM_E2E_OK` + `USER_SMOKE_OK`；四臂产物、union pool、归因图/API smoke 全部有 §4.1 落盘证据。
2. **B6 contract complete**（Wave 2 + 人判路径，符合 runbook §10）：**明确要求 T8 + T13a + T13b + T14**——四臂产物、union pool、人工判定协议与模板、最小可复查标注（12 例）、B6 分析报告（至少空骨架，由 T13b 提供真实判定输入）全部完成并校验；状态可更新为 `READY_FOR_FINAL_MANAGER_REVIEW`（仅由 manager 判定）。
3. **Application narrative ready**（Wave 4 + 诚实限制）：T13a/T13b–T15 完成，公开文档含限制声明，无未证实的准确度数字。

---

## §10 下一个 agent 的立即动作

**推荐范围：Implement T1–T3 only（Wave 1）。**
成功 token：`INDEX_WIRING_OK`。
输入：本文档 §4 T1–T3 + §4.1 Evidence contract + §7 cheat sheet；不改数据、不 re-embed、不 commit/push；
完成标准 = active 表被运行时打开、env 契约文档化、query embedding 策略文档化且单测通过，并在
`data/run_reports/post_import/<run_id>/` 落盘 `meta.json` + `WAVE_TOKEN.txt`。
