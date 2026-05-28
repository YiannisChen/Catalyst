> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# Provider Audit 7-Day Test Spec

**Purpose:** Validate data fetch stability and RAG-readiness before locking source policy.

## 1. Test Scope

- Tickers: `AAPL,NVDA,TSLA,MSFT,AMZN`
- Window: latest 7 trading days
- Sources: `polygon_news,polygon_ohlcv,fmp_fundamentals,fred_macro`
- News threshold: `min_news_chars=200`

## 2. Command

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/data-core
/Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -u -m scripts.provider_audit \
  --tickers AAPL,NVDA,TSLA,MSFT,AMZN \
  --days 7 \
  --sources polygon_news,polygon_ohlcv,fmp_fundamentals,fred_macro \
  --min-news-chars 200 \
  --progress-every 10 \
  --verbose-errors
```

## 3. Required Output Artifacts

- JSON report in `data/eval_reports/provider_audit_*.json`
- Markdown report in `data/eval_reports/provider_audit_*.md`

## 4. Metrics To Review

- Stability:
- `run_ok_rate`
- `endpoint_ok_rate`
- Data quality (news):
- `rag_eligible_rate`
- `median_article_chars`
- `missing_title_rate`
- `missing_published_rate`
- `missing_url_rate`
- Dedup quality (news):
- `raw_articles_total`
- `cleaned_articles_total`
- `dedup_removed_total`
- `duplicate_rate = 1 - cleaned/raw`
- Throughput/operational:
- `avg_endpoint_latency_ms`
- `avg_payload_bytes`

## 5. Acceptance Thresholds (v1)

- Non-news sources:
- `run_ok_rate >= 0.95`
- `endpoint_ok_rate >= 0.98`
- News source:
- `run_ok_rate >= 0.95`
- `rag_eligible_rate >= 0.60`
- `median_article_chars >= 200`
- `duplicate_rate` should be non-zero but bounded (typically < 0.40 unless major event day)

## 6. Failure Interpretation

- If all sources fail with DNS-like errors (for example: `[Errno 8] nodename nor servname provided, or not known`):
- Mark this run **invalid (environment/network issue)**.
- Do not use this run for source policy decisions.
- Rerun in a stable network environment and compare with prior valid run.

## 7. Decision Rule

- `primary_candidate`: stable + quality above thresholds.
- `fallback_only_unstable`: unstable fetch or repeated provider/network failures.
- `fallback_low_text_quality`: stable fetch but poor RAG text quality.

