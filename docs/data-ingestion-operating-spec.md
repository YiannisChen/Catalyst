# Catalyst Data Ingestion Operating Spec (v1)

**Status:** Proposed  
**Date:** 2026-04-26  
**Owner:** data-core

## 1. 目标与边界

这份文档定义两件事：
- 如何建立可用于 RAG 的历史数据库（`backfill`）。
- 如何每天稳定增量抓取（`daily sync`）。

边界说明：
- 目标是“公开信息下的可追溯解释系统”，不是全市场全量爬虫。
- 优先保证稳定、可复现、可回放，不追求无限扩源。

## 2. 关键决策（先拍板）

在做 source 规划前，先跑一次 provider 体检脚本（真实抓取样本）：

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/data-core
/Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m scripts.provider_audit \
  --tickers AAPL,NVDA,TSLA,MSFT,AMZN \
  --days 15 \
  --min-news-chars 200
```

脚本输出每个 source 的：
- 稳定性（run/endpoint 成功率）
- 文本长度分布
- 元数据缺失率
- 粗粒度 RAG 可用率（`rag_eligible_rate`）
- 推荐分级（`primary_candidate` / `fallback_*`）

source 取舍必须基于这份报告，而不是先入为主。

### 决策 A：不是“所有 source 全抓”

不采用“所有 provider 的所有 news 全部抓下来再统一清洗”的策略。  
采用 **Primary + Conditional Fallback**：
- 每类数据先选主源（Primary）。
- 覆盖不足或主源失败时才触发备用源（Fallback）。
- 通过去重合并后进入 Silver/Gold。

原因：
- 全抓会显著增加噪声、重复、成本和维护复杂度。
- 免费额度下更容易触发限流和不稳定行为。
- 对 RAG 质量提升不一定线性，反而可能拉低检索精度。

### 决策 B：短新闻不丢弃，但默认不入 RAG 主索引

短文本新闻常见问题：信息密度低、上下文不足、误导 reranker。  
策略：
- 先落库到 Bronze（保留审计能力）。
- 清洗后按质量门槛打标 `is_rag_eligible`。
- 不满足门槛的文本不进入 Gold 主检索索引，但可保留为辅助证据。

### 决策 C：回填与日更采用同一条 idempotent 管线

禁止“回填脚本一套、日更脚本一套”。  
同一条 orchestrator + pipeline，区别仅在时间窗口与调度策略。

## 3. 数据源策略

## 3.1 Source 类型分层

- `Market Structure`: OHLCV（价格、成交量）
- `Company Text`: 公司相关新闻/公告文本
- `Fundamental`: 财务/估值结构化数据
- `Macro/Event`: 宏观、政策、地缘等非 ticker 中心事件

## 3.2 推荐启用方式（v1）

- Primary:
- `polygon_ohlcv`
- `polygon_news`
- `fmp_fundamentals`
- Conditional Fallback:
- `fmp_news` 或 `finnhub_company_news`（主源覆盖不足时触发）
- `gdelt_news`（宏观层检索需要时启用）

不建议 v1 默认开启所有新闻源并行抓取。

## 3.3 Fallback 触发条件

在某个 ticker/date 触发以下任一条件时，启用 fallback 新闻源：
- 主源返回空且该日价格波动绝对值 >= 阈值（如 3%）。
- 主源连续失败（429/5xx/timeout）达到阈值。
- 主源返回文本全部未通过 `is_rag_eligible`。

## 4. 数据库建立（1年回填）

## 4.1 回填范围

- 默认窗口：`T-365` 到 `T-1`。
- 先跑核心 ticker 列表（如 10-30 个），通过质量门槛后再扩 universe。
- 回填按日分片，避免单次任务过大不可恢复。

## 4.2 运行机制

每次回填 run 必须记录：
- `run_id`
- 时间窗口
- ticker 列表
- source 列表
- 各阶段成功/失败计数
- 成本与耗时

推荐新增表：
- `ingestion_runs`
- `source_checkpoints`
- `asset_quality_flags`

## 4.3 Checkpoint 与可恢复

Checkpoint 粒度建议：`(source_type, ticker, date)`  
状态：`pending | success | failed | skipped`  
回填重启时：
- 只重跑 `pending/failed`。
- `success` 记录不重复抓取。

## 4.4 幂等写入

必须使用稳定主键和 upsert 语义（当前已有 `INSERT OR REPLACE` 基础）。  
建议统一资产键：
- `asset_id = sha256(source_type + canonical_url + published_utc + ticker)`

## 5. 每日增量抓取（Daily Sync）

## 5.1 调度

- 每日固定 1 次主任务（美股收盘后）。
- 增加 1 次小窗口补偿任务（次日），处理延迟发布与修订。

## 5.2 增量窗口

- 主任务：`D-1`。
- 补偿任务：`D-2 ~ D` 滑动重扫（小窗口、幂等覆盖）。

## 5.3 日常流程

1. 读取当天 ticker universe。  
2. 跑 primary sources。  
3. 根据覆盖与质量触发 conditional fallback。  
4. 清洗、去重、打质量标。  
5. 仅将 `is_rag_eligible` 的 canonical 文档入 Gold。  
6. 生成日报（覆盖、失败、质量、成本）。

## 6. 质量控制（解决“新闻太短无法RAG”）

## 6.1 质量门槛（建议初始值）

- `min_char_count >= 200`（低于该值默认不入主索引）。
- 必须包含最小元数据：`title`, `published_utc`, `source`.
- 非目标语言文本标记为不入主索引（可保留原文）。
- 明显模板/广告/重复摘要降权或剔除。

以上阈值应配置化，不要硬编码。

## 6.2 质量标签

建议新增字段或侧表：
- `is_rag_eligible`（bool）
- `quality_reason`（enum: short_text/missing_fields/duplicate/spam_like/...）
- `quality_score`（0-1，可选）

## 6.3 覆盖与质量监控指标

- `coverage_rate`: 每日有至少1条可用文本的 ticker 占比
- `eligibility_rate`: 抓取文本中通过质量门槛的比例
- `duplicate_rate`: 清洗后被判重比例
- `empty_on_big_move_rate`: 大波动日却无可用文本的比例

## 7. 清洗与去重稳定性

## 7.1 去重策略（v1）

- Hard dedup（已实现主路径）：
- 标题归一化 + 2小时窗口指纹
- Cross-source URL 归一化判重（建议补）

## 7.2 去重顺序

1. Source 内去重（同源重复）。  
2. 跨源去重（同事件多源转载）。  
3. 保留 canonical 记录，其他记录挂 `canonical_asset_id`（建议补）。  

## 7.3 语义去重（v1.1）

当 hard dedup 稳定后再加 semantic dedup（向量相似度阈值），避免过早复杂化。

## 8. RAG 入库策略

Gold 索引不要无脑收录所有文本。  
仅入索引：
- `is_rag_eligible=True`
- `is_duplicate=False`（或 canonical）
- 元数据完整可追溯

短新闻处理：
- 存 Bronze/Silver，默认不入 Gold 主索引。
- 可在召回不足时作为低权重补充候选。

## 9. API Key 与配额治理

不使用多账号绕过 provider 限额或 ToS。  
推荐策略：
- 合规 key 池：用于容灾、轮换、环境隔离，不用于规避限制。
- 严格限流与 daily budget。
- 配额不足时优先做“范围收缩 + 缓存 + fallback”，而不是扩大账号规模。

原因：
- 法务/封号风险
- 可复现性差，无法作为工程资产沉淀
- 开源和答辩阶段难以自洽

## 10. 可靠性与验收标准

v1 运行稳定门槛（建议）：
- 日任务成功率 >= 99%（run 维度）
- `coverage_rate` >= 85%（核心 ticker）
- `eligibility_rate` >= 60%（文本源）
- 失败任务支持 checkpoint 重跑，无重复脏写
- 每日生成 ingestion report（成功/失败/覆盖/质量/成本）

## 11. 开发落地顺序（2周）

Week 1:
- 新增 `ingestion_runs` + `source_checkpoints` + 报表骨架
- 实现回填 checkpoint 重跑
- 实现 `is_rag_eligible` 与质量原因标记

Week 2:
- 实现 conditional fallback 策略
- 增加跨源 URL 归一化去重
- 打通 daily sync + 补偿任务 + 指标报表

## 12. 与现有文档关系

- 本文是数据摄取与运维执行规范。
- Agent/评测总体设计参考：
- `docs/full-version-execution-spec.md`
- 细节决策采用 ADR 增补，不回写到白皮书作为执行依据。
