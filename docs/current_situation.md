# Catalyst Current Situation (for new Claude session)

**Last updated:** 2026-04-13  
**Purpose:** 快速让新会话了解当前状态，并在此基础上给出改进建议。  
**Important framing:** 当前对外定位是“以金融事件解释为载体的 agent engineering showcase”，不是金融归因产品。

---

## 1. Midterm -> Defense -> Roadmap（状态分层）

### 1.1 Midterm freeze（历史基线）
- 中期版本是 script-driven research prototype。
- 能力边界：`packages/data-core`、`packages/eval`、`packages/agents` 可跑通，但不宣称 API/MCP/前端产品化。
- 结论：midterm 证明链路可跑，不代表系统已具备可审计、可拒答、可回放的控制平面能力。

### 1.2 Defense now（当前建设目标）
- 防守目标不是“比 Claude 答得更聪明”，而是把高不确定问题做成可治理系统。
- P0 核心增量：`Validator + 四态输出 + trace 持久化 + direct_llm baseline + gate 化评测`。
- 关键可见信号：每条 claim 可追溯 evidence，证据不足时显式 `PARTIAL/INSUFFICIENT`，失败沉淀为 taxonomy + regression tests。

### 1.3 Roadmap（答辩后）
- 延伸方向：Layer 3 retrieval、budget circuit breaker、LangSmith 对齐、更完整 eval matrix。
- 对外叙事：金融是高噪声测试域，不是产品商业方向；工程能力才是资产。

---

## 2. Midterm version 现状（已实现与边界）

### 1.1 Midterm 的准确定位
- 当前中期版本是 **script-driven research prototype**。
- 主要由三个包组成：
  - `packages/data-core`（数据采集与存储）
  - `packages/eval`（评估与对比）
  - `packages/agents`（Miner-Critic-Judge 归因链路）
- 中期不应宣称已完成 API / MCP / 前端产品化。

### 1.2 中期已打通的能力（可演示）
- Bronze/Silver 数据链路可跑通：`polygon_news`、`polygon_ohlcv`、`fmp_fundamentals`。
- SQLite 持久化 `raw_assets` / `clean_assets`。
- LanceDB Gold 索引可构建，支持 hybrid retrieval（BM25 + vector）。
- Agent graph 支持：
  - `Miner -> Critic -> Judge`（默认）
  - `Miner -> Judge`（ablation baseline）
- Eval harness 已实现 5 个指标：
  - `attribution_f1`
  - `category_accuracy`
  - `grounding_rate`
  - `temporal_precision`
  - `confidence_calibration`
- 有 canonical acceptance path：`scripts/e2e_strict.py`。

### 1.3 中期已知限制（必须如实陈述）
- strict acceptance 流程仍较窄：单案例/小语料验证为主。
- 检索栈尚未完全落到 full design 的目标状态（strict path 仍存在 embedding/reranker 取舍差异）。
- demo 是脚本化操作，不是用户侧完整产品体验。
- 运行依赖外部 API key 和上游服务可用性。

---

## 2. Full version 设计目标（end-state architecture）

### 2.1 目标问题
- 核心问题：**“Why did this stock move on a given day?”**
- 目标是可审计、可评测的归因系统，而不是单次聊天回答。

### 2.2 完整形态（架构层）
- 数据层：多源金融数据接入、清洗、对齐、去重、存储。
- 检索层：Hybrid RAG（BM25 + vector + reranker）。
- Agent 层：Miner-Critic-Judge（含可对照 ablation）。
- 评估层：golden set + 指标体系 + experiment harness。
- 产品层（post-midterm）：API + 前端 dashboard + MCP。

### 2.3 Full version 的关键设计原则
- 技术决策链（每个技术选择都要有业务理由）。
- Eval 先行，不以“看起来合理”替代可量化验证。
- 证据不足时允许拒答/降级，不强行生成归因。
- 可观测、可回放、可比较（baseline vs full pipeline）。

---

## 3. 导师提出的问题（按本次要求，去掉 A 股相关）

以下问题是当前必须正面回应的核心挑战：

1. **研究意义问题**  
   “通用大模型（GPT/Claude）也能给类似解释，你这套系统的研究和实用价值在哪里？”

2. **落地可行性问题**  
   “依赖外部大模型 API，成本高、不可控、难本地部署，这条路线在真实场景是否可行？”

3. **检索遗漏问题**  
   “按公司过滤后再检索会漏掉跨主体证据，导致证据不足。怎么解决？”

4. **结果质量提升问题**  
   “目前只完成流程验证、质量一般，后续怎样系统性提升准确性？”

---

## 4. DY 提出的“Agent 项目是不是 Toy”判据（与 Catalyst 的相关性）

DY 提供的是工程化审查视角，核心是在问：这是“可持续系统”还是“一次性 demo”。

### 4.1 关键审查点（提炼版）
- Agent 能否自己判断是否检索、查什么、查到什么程度停止。
- 是否有长期/短期记忆机制（若场景需要）。
- 是否有可量化评估体系并能回答“比 baseline 强多少”。
- 是否具备真正的多角色分工，而非单 prompt 包打天下。
- 工具失败后是否能自动恢复，而不是人工重跑。
- 中间结果不对时，是否会动态调整计划。
- 是否具备安全治理：最小权限、风险分级、审计日志、注入防护。

### 4.2 对 Catalyst 的现实映射（客观）
- 已具备的非-toy基础：
  - 有 MCJ 分工与 graph 结构。
  - 有 eval 指标体系与 baseline ablation 入口。
- 仍偏 toy 的风险点：
  - 检索策略还不够自适应（跨主体扩检能力不足）。
  - failure harness 不够硬（解析失败/工具失败后的恢复策略需强化）。
  - 与通用大模型 direct-answer 的系统对照实验仍需补强。

---

## 5. 当前共识：最优先改进方向（供新会话直接展开）

1. **检索升级优先级最高**  
   从“单 ticker 过滤检索”升级为“direct evidence + related entities + market fallback”的分层检索。

2. **强化证据充分性与拒答机制**  
   明确输出“可解释/部分可解释/不可充分解释”，避免证据不足时硬归因。

3. **补齐 baseline 对照实验**  
   固定比较：通用大模型直答、无 Critic、有 Critic、完整链路；统一输出质量/成本/时延指标。

4. **把失败沉淀为 harness 规则**  
   将 schema 失败、工具失败、解析失败转化为可复用的自动恢复与审计机制，而不是仅调 prompt。

---

## 6. 给新 Claude 的执行建议

建议新会话直接按以下顺序工作：

1. 先输出“问题-改进映射表”（每个问题对应可实施设计改动）。
2. 再给出“4 周可落地路线图”（每周有可验收产物和指标）。
3. 最后给“答辩话术模板”（强调中期边界 + full version 价值 + 非 toy 证据）。
