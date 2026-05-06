# T03-prea Chunking Profiling (Analysis Only)

Date: 2026-05-07
Scope: analysis-only support for T03-preb/T03 parameter locking. No `packages/**` code changes.

## Inputs & Method
- DB: `data/catalyst_eval_frozen_v2.db` (read-only)
- Table: `clean_assets`
- L1 baseline for inflation: `is_duplicate=0 AND LENGTH(content_md)>0` (count = 13474)
- Splitter comparison sample: 100 `polygon_news` rows from L1 scope, deterministic sample (`seed=42`)

## 1) `source_type` 画像（clean_assets）

| source_type | rows | min | median | p75 | p95 | max |
|---|---:|---:|---:|---:|---:|---:|
| fmp_fundamentals | 3500 | 5131 | 5220.5 | 5249.0 | 5297.0 | 5297 |
| fred_macro | 3500 | 6028 | 9434.0 | 9635.0 | 9739.0 | 9744 |
| polygon_ohlcv | 3349 | 166 | 177.0 | 179.0 | 182.0 | 188 |
| polygon_news | 3125 | 387 | 3551.0 | 6304.0 | 12550.4 | 31486 |

## 2) Splitter 比较（100条 `polygon_news`）

Splitters:
- spaCy `en_core_web_sm`
- regex `r'(?<=[.!?])\s+(?=[A-Z"\'])'`
- NLTK `punkt_tab`

### Pathological split rate 定义（本次口径）
按「asset 级」统计；若该 asset 出现任一异常则计入 pathological：
- `sentence_count == 0`
- `sentence_count > 120`
- 存在短碎片：`len(sentence) <= 8` 且字母数 `>= 4`
- 存在超长句：`len(sentence) >= 1200`
- 存在仅标点碎片

### 统计结果

| splitter | min | median | p75 | p95 | max | mean | pathological rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| spaCy `en_core_web_sm` | 3 | 18.0 | 31.25 | 54.05 | 116 | 23.42 | 30% |
| regex boundary | 1 | 7.0 | 14.25 | 29.10 | 68 | 10.71 | 49% |
| NLTK `punkt_tab` | 3 | 16.0 | 26.25 | 49.05 | 117 | 20.39 | 26% |

Pathology breakdown:
- spaCy: `short_fragment=28`, `overlong_sentence=2`
- regex: `overlong_sentence=49`
- NLTK: `overlong_sentence=26`

### 代表性错误样例（5条）
1. `asset_id=2253975a...`（Motley Fool 聚合段）
- spaCy: 把标题拆成 `## NVDA:` 短碎片（short_fragment）
- regex: `U.S.`/`vs.` 等缩写附近切分偏少，保留较长段（overlong）
- NLTK: 句数最接近 spaCy，但未产生标题短碎片

2. `asset_id=7c381115...`（ROG/RTX 新闻拼接）
- regex: 多条新闻拼接后跨段切分不足（overlong）
- NLTK: 同样存在超长段，但较 regex 更细
- spaCy: 句粒度更细，但对 markdown 元数据边界不稳定

3. `asset_id=d5a428c6...`（Top stocks 列表文）
- spaCy: 标题前缀被拆成极短句（short_fragment）
- regex/NTLK: 能保留完整标题句，但在多段拼接处出现长句

4. `asset_id=b2f7b047...`（Q3 earnings 预告）
- spaCy/NTLK: 标题问句被拆成两句（可接受）
- regex: 句数显著更低且保留长段（overlong）

5. `asset_id=c4a280f6...`（Healthcare AI 报告）
- regex: 第一段过长（overlong）
- NLTK: 句界更平衡，未出现短碎片
- spaCy: 句数偏高，标题行切分颗粒更碎

## 3) L2 膨胀估算

Assumption for this pre-analysis estimate:
- splitter candidate: `nltk_punkt_tab`
- L2 eligible source types candidate: `['polygon_news']`（保守起步）

| max_sentences_per_asset | L2 rows | L1+L2 total rows | expansion multiple |
|---|---:|---:|---:|
| 20 | 41579 | 55053 | 4.0859x |
| 30 | 49731 | 63205 | 4.6909x |
| 50 | 56967 | 70441 | 5.2279x |

Sensitivity (`polygon_news + fred_macro`):
- cap=20: `4.3456x`
- cap=30: `4.9506x`
- cap=50: `5.4877x`

## 4) 参数建议（Candidate Only, 非最终锁定）

| 参数 | Candidate | 说明 |
|---|---|---|
| splitter | `nltk_punkt_tab`（主候选） | 在本样本下 pathological rate 最低（26%），且无明显标题短碎片放大问题 |
| max_sentences_per_asset | `20`（主候选），`30`（备选），`50`（高召回上限） | `20` 可控性更好；`30/50` 对行数与后续成本放大明显 |
| min_sentence_length | `20`（主候选），`12`（备选） | 用于过滤极短标题碎片/噪声段；需在 pre-b 看召回损失 |
| L2 eligible source types | `['polygon_news']`（主候选） | `fred_macro` 可作为 pre-b 扩展候选，但会进一步放大体量 |

## 明确边界
- 本文结论为 **candidate only**。
- **final lock 必须在 T03-preb**：结合 T02 后真实检索分布、命中质量和成本数据再定。

## 给 T03-preb 的待决事项
1. 基于 T02 真检索分布，评估 `splitter × max_sentences × min_sentence_length × eligible_types` 的召回/精度变化。
2. 量化每组参数在 frozen cases 上的证据覆盖变化（尤其 direct/macro 分层）。
3. 评估索引体量、查询延迟、rerank/token 成本，并设定可接受上限。
4. 确认是否纳入 `fred_macro`，以及是否需要 source_type-specific cap（例如 news=20, macro=10）。
5. 形成最终锁定参数并回写到 T03 实现任务（本任务不落地实现）。
