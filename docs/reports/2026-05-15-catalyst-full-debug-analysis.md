# 2026-05-15 Catalyst Full Debug Analysis (No New Runs)

## Terminology Contract

System phase: P0/P1/P2
Remediation phase: R0/R1/R2

## Executive summary

- 数据来源严格限定为既有产物（主 run、retry run、refactor 对照 run 的 core/summaries/traces）。
- 主 run 199 + retry 1 = 200，`(profile, case_id)` 唯一性校验通过。
- K=2 overall status_acc = **0.77**；K=3 overall = **0.54**；绝对提升 **0.23**（相对 42.6%）。
- K=2 full=0.86，degraded=0.70，full-degraded gap=0.16。
- K=2 SYSTEM_ERROR=0；错误样本总量=46。

## Metrics comparison（K=3 vs K=2）

| profile | K=3 acc | K=2 acc | abs delta | rel delta | K=3 system_error | K=2 system_error |
|---|---:|---:|---:|---:|---:|---:|
| full | 0.60 | 0.86 | +0.26 | +43.3% | 0 | 0 |
| no_rerank | 0.48 | 0.70 | +0.22 | +45.8% | 0 | 0 |
| no_vector | 0.58 | 0.82 | +0.24 | +41.4% | 1 | 0 |
| degraded | 0.50 | 0.70 | +0.20 | +40.0% | 0 | 0 |
| overall | 0.54 | 0.77 | +0.23 | +42.6% | 1 | 0 |

### K=2 output_status distribution
- full: SUFFICIENT=45, PARTIAL=2, INSUFFICIENT=3, SYSTEM_ERROR=0
- no_rerank: SUFFICIENT=37, PARTIAL=10, INSUFFICIENT=3, SYSTEM_ERROR=0
- no_vector: SUFFICIENT=43, PARTIAL=4, INSUFFICIENT=3, SYSTEM_ERROR=0
- degraded: SUFFICIENT=35, PARTIAL=11, INSUFFICIENT=4, SYSTEM_ERROR=0
- overall: SUFFICIENT=160, PARTIAL=27, INSUFFICIENT=13, SYSTEM_ERROR=0

## Failure taxonomy（按首责层）

| first_failure_point | count | share |
|---|---:|---:|
| CRITIC_GATE | 15 | 32.6% |
| DATA_COVERAGE_GAP | 12 | 26.1% |
| RETRIEVAL_RERANK | 8 | 17.4% |
| GOLDEN_LABEL_AMBIGUITY | 7 | 15.2% |
| MINER_COVERAGE | 3 | 6.5% |
| VALIDATOR_FINALIZER | 1 | 2.2% |

逐 case 证据链详见 `2026-05-15-catalyst-error-taxonomy.csv`。

## 20-case deep dives

1. 问题: full/g015 SUFFICIENT→INSUFFICIENT（DATA_COVERAGE_GAP）
   证据: expected=SUFFICIENT, output=INSUFFICIENT; critic(all=8, below=8, top=0.2); retrieval(graded=0, reranked=8); judge(validation_error=None, grounding_rate_field=None)
   建议: 对标记 coverage_gap 的样本单独打分或补充来源，避免把数据空洞计入模型能力缺陷。
2. 问题: full/g046 SUFFICIENT→INSUFFICIENT（DATA_COVERAGE_GAP）
   证据: expected=SUFFICIENT, output=INSUFFICIENT; critic(all=8, below=8, top=0.2); retrieval(graded=0, reranked=8); judge(validation_error=None, grounding_rate_field=None)
   建议: 对标记 coverage_gap 的样本单独打分或补充来源，避免把数据空洞计入模型能力缺陷。
3. 问题: full/g049 SUFFICIENT→INSUFFICIENT（DATA_COVERAGE_GAP）
   证据: expected=SUFFICIENT, output=INSUFFICIENT; critic(all=8, below=8, top=0.2); retrieval(graded=0, reranked=8); judge(validation_error=None, grounding_rate_field=None)
   建议: 对标记 coverage_gap 的样本单独打分或补充来源，避免把数据空洞计入模型能力缺陷。
4. 问题: no_vector/g015 SUFFICIENT→INSUFFICIENT（DATA_COVERAGE_GAP）
   证据: expected=SUFFICIENT, output=INSUFFICIENT; critic(all=8, below=8, top=0.3); retrieval(graded=0, reranked=8); judge(validation_error=None, grounding_rate_field=None)
   建议: 对标记 coverage_gap 的样本单独打分或补充来源，避免把数据空洞计入模型能力缺陷。
5. 问题: no_vector/g049 SUFFICIENT→INSUFFICIENT（DATA_COVERAGE_GAP）
   证据: expected=SUFFICIENT, output=INSUFFICIENT; critic(all=8, below=8, top=0.2); retrieval(graded=0, reranked=8); judge(validation_error=None, grounding_rate_field=None)
   建议: 对标记 coverage_gap 的样本单独打分或补充来源，避免把数据空洞计入模型能力缺陷。
6. 问题: no_vector/g046 SUFFICIENT→INSUFFICIENT（DATA_COVERAGE_GAP）
   证据: expected=SUFFICIENT, output=INSUFFICIENT; critic(all=8, below=8, top=0.1); retrieval(graded=0, reranked=8); judge(validation_error=None, grounding_rate_field=None)
   建议: 对标记 coverage_gap 的样本单独打分或补充来源，避免把数据空洞计入模型能力缺陷。
7. 问题: no_rerank/g015 SUFFICIENT→INSUFFICIENT（DATA_COVERAGE_GAP）
   证据: expected=SUFFICIENT, output=INSUFFICIENT; critic(all=7, below=7, top=0.4); retrieval(graded=0, reranked=8); judge(validation_error=None, grounding_rate_field=None)
   建议: 对标记 coverage_gap 的样本单独打分或补充来源，避免把数据空洞计入模型能力缺陷。
8. 问题: no_rerank/g046 SUFFICIENT→INSUFFICIENT（DATA_COVERAGE_GAP）
   证据: expected=SUFFICIENT, output=INSUFFICIENT; critic(all=8, below=8, top=0.4); retrieval(graded=0, reranked=8); judge(validation_error=None, grounding_rate_field=None)
   建议: 对标记 coverage_gap 的样本单独打分或补充来源，避免把数据空洞计入模型能力缺陷。
9. 问题: no_rerank/g049 SUFFICIENT→INSUFFICIENT（DATA_COVERAGE_GAP）
   证据: expected=SUFFICIENT, output=INSUFFICIENT; critic(all=8, below=8, top=0.1); retrieval(graded=0, reranked=8); judge(validation_error=None, grounding_rate_field=None)
   建议: 对标记 coverage_gap 的样本单独打分或补充来源，避免把数据空洞计入模型能力缺陷。
10. 问题: degraded/g015 SUFFICIENT→INSUFFICIENT（DATA_COVERAGE_GAP）
   证据: expected=SUFFICIENT, output=INSUFFICIENT; critic(all=8, below=8, top=0.3); retrieval(graded=0, reranked=8); judge(validation_error=None, grounding_rate_field=None)
   建议: 对标记 coverage_gap 的样本单独打分或补充来源，避免把数据空洞计入模型能力缺陷。
11. 问题: degraded/g046 SUFFICIENT→INSUFFICIENT（DATA_COVERAGE_GAP）
   证据: expected=SUFFICIENT, output=INSUFFICIENT; critic(all=8, below=8, top=0.2); retrieval(graded=0, reranked=8); judge(validation_error=None, grounding_rate_field=None)
   建议: 对标记 coverage_gap 的样本单独打分或补充来源，避免把数据空洞计入模型能力缺陷。
12. 问题: degraded/g049 SUFFICIENT→INSUFFICIENT（DATA_COVERAGE_GAP）
   证据: expected=SUFFICIENT, output=INSUFFICIENT; critic(all=8, below=8, top=0.2); retrieval(graded=0, reranked=8); judge(validation_error=None, grounding_rate_field=None)
   建议: 对标记 coverage_gap 的样本单独打分或补充来源，避免把数据空洞计入模型能力缺陷。
13. 问题: degraded/g050 SUFFICIENT→INSUFFICIENT（MINER_COVERAGE）
   证据: expected=SUFFICIENT, output=INSUFFICIENT; critic(all=8, below=8, top=0.2); retrieval(graded=0, reranked=8); judge(validation_error=None, grounding_rate_field=None)
   建议: 对 critic=insufficient 但 top_relevance 较高样本增加一次 proceed 分支复核，比较误拒率变化。
14. 问题: full/g023 SUFFICIENT→PARTIAL（CRITIC_GATE）
   证据: expected=SUFFICIENT, output=PARTIAL; critic(all=8, below=7, top=1.0); retrieval(graded=1, reranked=8); judge(validation_error=None, grounding_rate_field=1.0)
   建议: 引入“高相关单证据保留”策略（top_relevance>=0.8 且 grounding=1.0 时避免过早降为 PARTIAL/INSUFFICIENT）。
15. 问题: full/g050 SUFFICIENT→PARTIAL（CRITIC_GATE）
   证据: expected=SUFFICIENT, output=PARTIAL; critic(all=8, below=7, top=0.9); retrieval(graded=1, reranked=8); judge(validation_error=None, grounding_rate_field=1.0)
   建议: 引入“高相关单证据保留”策略（top_relevance>=0.8 且 grounding=1.0 时避免过早降为 PARTIAL/INSUFFICIENT）。
16. 问题: no_vector/g023 SUFFICIENT→PARTIAL（CRITIC_GATE）
   证据: expected=SUFFICIENT, output=PARTIAL; critic(all=8, below=7, top=1.0); retrieval(graded=1, reranked=8); judge(validation_error=None, grounding_rate_field=1.0)
   建议: 引入“高相关单证据保留”策略（top_relevance>=0.8 且 grounding=1.0 时避免过早降为 PARTIAL/INSUFFICIENT）。
17. 问题: no_vector/g031 SUFFICIENT→PARTIAL（MINER_COVERAGE）
   证据: expected=SUFFICIENT, output=PARTIAL; critic(all=8, below=7, top=1.0); retrieval(graded=1, reranked=8); judge(validation_error=None, grounding_rate_field=1.0)
   建议: 提高该 profile 的候选覆盖（增加 direct/L1 召回上限或扩展窗口），并验证 graded_total 是否从 1 提升到 >=2。
18. 问题: no_vector/g027 SUFFICIENT→PARTIAL（MINER_COVERAGE）
   证据: expected=SUFFICIENT, output=PARTIAL; critic(all=8, below=7, top=0.9); retrieval(graded=1, reranked=8); judge(validation_error=None, grounding_rate_field=1.0)
   建议: 提高该 profile 的候选覆盖（增加 direct/L1 召回上限或扩展窗口），并验证 graded_total 是否从 1 提升到 >=2。
19. 问题: no_vector/g050 SUFFICIENT→PARTIAL（CRITIC_GATE）
   证据: expected=SUFFICIENT, output=PARTIAL; critic(all=8, below=7, top=0.9); retrieval(graded=1, reranked=8); judge(validation_error=None, grounding_rate_field=1.0)
   建议: 引入“高相关单证据保留”策略（top_relevance>=0.8 且 grounding=1.0 时避免过早降为 PARTIAL/INSUFFICIENT）。
20. 问题: no_rerank/g023 SUFFICIENT→PARTIAL（CRITIC_GATE）
   证据: expected=SUFFICIENT, output=PARTIAL; critic(all=8, below=7, top=1.0); retrieval(graded=1, reranked=8); judge(validation_error=None, grounding_rate_field=1.0)
   建议: 引入“高相关单证据保留”策略（top_relevance>=0.8 且 grounding=1.0 时避免过早降为 PARTIAL/INSUFFICIENT）。

## Practical verdict: Catalyst useful or not, and conditions

**结论：有用，但仅在 full 配置与当前 K=2 校准下显著；弱配置与覆盖空洞场景下收益下降。**

有效能力：
- 在 full profile 下，status_acc 从 K=3 的 0.60 提升到 K=2 的 0.86（+0.26）。
- full-degraded gap 为 0.16，说明分层检索/重排链路对最终状态判断有可测增益。
- K=2 合并后 SYSTEM_ERROR=0（K=3 对照在 no_vector 有 1 例），工程稳定性改善。

短板/高成本环节：
- 46/200 样本仍错误（23%），其中 6 例为 Critic gate 相关、12 例为数据覆盖空洞。
- 错误主模式为 SUFFICIENT->PARTIAL (26) 与 SUFFICIENT->INSUFFICIENT (13)。
- 相对直接问通用大模型，当前短板是门控链路叠加导致的保守性偏置；优势是证据可追溯与分层可诊断。

## ROI-ranked action plan

| ROI | action | expected impact | validation |
|---|---|---|---|
| 高 | Critic 单证据高置信豁免（门控改进） | 减少 SUFFICIENT->PARTIAL/INSUFFICIENT 误拒，预计提升 full status_acc 3-6pt。 | 对照 A/B：比较 gate 前后 full/degraded 的误拒数与 grounding_rate 变化。 |
| 高 | 数据覆盖 gap 单独计分与告警 | 把 coverage 空洞与模型缺陷解耦，预计减少 12 例误判对主指标污染。 | 新增覆盖分层指标：含/不含 data_coverage_gap 的 status_acc 双报表。 |
| 高 | no_rerank 退化补偿策略 | 针对 8 例 RETRIEVAL_RERANK 错误，提升无重排场景鲁棒性。 | 仅在 no_rerank profile 回放，目标 status_acc 从 0.70 提升到 >=0.76。 |
| 中 | degraded/no_vector 最小召回增强 | 缓解 11 例 MINER_COVERAGE，提升弱配置稳定性。 | 比较 ablation 中 degraded/no_vector 的 graded_total>=2 比率。 |
| 中 | Validator evidence_id 修复与回归 | 清除格式性降级（已见 1 例）。 | 新增单测并验证 validation_error 样本数归零。 |
| 低 | Golden label ambiguity 周期校准 | 减少 PARTIAL/SUFFICIENT 边界噪声（7 例）。 | 每轮抽样复审并记录标签变更影响。 |
