# Fixture index

Fixtures capture API responses through TestClient unless marked synthetic. Story presentation metadata is separate from the validated API response. SSE fixtures pair a JSON manifest with the `.jsonl` response sequence.

| File | Page state | Source | Request |
| --- | --- | --- | --- |
| `events-t1-aapl.json` | Persisted T1-AAPL SSE event sequence | `76f91456158d4c3fa1646261ebb65ead` | GET `/api/live-runs/{run_id}/stream` (76f91456158d4c3fa1646261ebb65ead) |
| `events-t3-now.json` | Persisted T3-NOW SSE event sequence | `8c2ba2d33e8d46f1b605fc7b48f22b16` | GET `/api/live-runs/{run_id}/stream` (8c2ba2d33e8d46f1b605fc7b48f22b16) |
| `page-a-abstain-leads-only.json` | Abstain with leads only | `synthetic from data/m9/eval_runs/b3_smoke/runtime.sqlite3` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-a-abstain-possible-explanation.json` | Abstain with a possible explanation | `synthetic from data/m9/eval_runs/b3_smoke/runtime.sqlite3` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-a-abstain-with-nothing.json` | Abstain with nothing | `2b050725848d4396a2aac28721963457` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-a-abstained-ceiling_abstain.json` | ABSTAINED: CEILING_ABSTAIN | `a5547b14b1834e6a9608363ce07f58c5` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-a-abstained-no_primary_proposed.json` | ABSTAINED: NO_PRIMARY_PROPOSED | `2b050725848d4396a2aac28721963457` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-a-abstained-primary_quotes_failed.json` | ABSTAINED: PRIMARY_QUOTES_FAILED | `dd7caf9db422454a9deb88f2aff26e8b` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-a-answered-now-b5.json` | T3 ANSWERED with primary and secondary quotes | `8c2ba2d33e8d46f1b605fc7b48f22b16` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-a-cost-unavailable.json` | Cost unavailable | `581b48d9a9814e5bbee1375251b8f204` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-a-grade-confirmed-x15.json` | event_time_v1 CONFIRMED with attribution and event evidence | `real grade_smoke1 x15` | GET `/api/live-runs/{run_id}/view` |
| `page-a-grade-no-clear-hon.json` | event_time_v1 NO_CLEAR_CAUSE; rejected HONA evidence is absent from possible explanations | `real grade_smoke2 x60/HON after Step 2 subject and abstention filters` | GET `/api/live-runs/{run_id}/view` |
| `page-a-grade-reported-x15.json` | event_time_v1 REPORTED contract projection | `derived one-document subset of real grade_smoke1 x15` | GET `/api/live-runs/{run_id}/view` |
| `page-a-dark-theme.json` | Dark theme | `8c2ba2d33e8d46f1b605fc7b48f22b16` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-a-headlines-loading.json` | Session selected, headlines loading | `data/m9/news_bodies_v2.t4snap_20261008.sqlite3` | GET `/api/sessions/{ticker}/{session_date}/news` with recorded params |
| `page-a-long-quote.json` | Long quote of at least 900 characters | `synthetic from data/m9/eval_runs/b5_smoke/runtime.sqlite3` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-a-news-empty.json` | Session selected, no headlines | `synthetic from data/m9/news_bodies_v2.t4snap_20261008.sqlite3` | GET `/api/sessions/{ticker}/{session_date}/news` with recorded params |
| `page-a-news-images-pending.json` | News list with images and images_pending | `data/m9/news_bodies_v2.t4snap_20261008.sqlite3` | GET `/api/sessions/{ticker}/{session_date}/news` with recorded params |
| `page-a-no-session-selected.json` | No session selected | `application model catalog` | GET `/api/models/catalog` |
| `page-a-old-run-no-trace.json` | Old run with no trace | `76f91456158d4c3fa1646261ebb65ead` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-a-one-primary-quote.json` | Answer with one primary quote | `synthetic from data/m9/eval_runs/b5_smoke/runtime.sqlite3` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-a-partial-legacy.json` | PARTIAL legacy run | `19cf478edcd346b3b6c69e73341cd3ed` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-a-run-cancelled.json` | Run cancelled | `synthetic from data/m9/eval_runs/b5_smoke/runtime.sqlite3` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-a-run-failed.json` | Run failed | `581b48d9a9814e5bbee1375251b8f204` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-a-running-stage-1.json` | Run streaming at stage 1 | `synthetic from data/m9/eval_runs/b5_smoke/runtime.sqlite3` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-a-running-stage-3.json` | Run streaming at stage 3 | `synthetic from data/m9/eval_runs/b5_smoke/runtime.sqlite3` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-a-running-stage-5.json` | Run streaming at stage 5 | `synthetic from data/m9/eval_runs/b5_smoke/runtime.sqlite3` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-a-tier-only-aapl-2026-03-24.json` | T1 TIER_ONLY | `76f91456158d4c3fa1646261ebb65ead` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-a-web-leads-cached.json` | Cached web leads (no new search) | `data/m9/web_search_smoke.sqlite3` | POST `/api/sessions/{ticker}/{session_date}/web-leads` with recorded params with recorded JSON body |
| `page-a-web-source-supported.json` | WEB_SOURCE_SUPPORTED | `19197473311449cbb25a4b0599981acc` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-a-width-1200.json` | 1200px viewport | `8c2ba2d33e8d46f1b605fc7b48f22b16` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-a-width-375.json` | 375px viewport | `8c2ba2d33e8d46f1b605fc7b48f22b16` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-a-width-768.json` | 768px viewport | `8c2ba2d33e8d46f1b605fc7b48f22b16` | GET `/api/live-runs/{run_id}/view` with recorded params |
| `page-data-job-latest.json` | Data job latest | `data/m9/update_jobs.sqlite3` | GET `/api/data/jobs/latest` |
| `page-data-overview.json` | Data center overview (60 sessions, synthetic) | `synthetic realistic sample` | GET `/api/data/overview?sessions=60` |
| `page-featured-list.json` | Featured list with NOW, AAPL tier-only, and no-clear-cause examples | `synthetic, from events-t3-now / page-a-answered-now-b5` | GET `/api/featured` |
| `page-featured-detail-now.json` | Featured NOW case detail | `synthetic, from events-t3-now / page-a-answered-now-b5` | GET `/api/featured/{case_id}` |
| `page-featured-replay-now.json` | Featured NOW public event replay | `synthetic, from events-t3-now / page-a-answered-now-b5` | GET `/api/featured/{case_id}/replay` |
| `page-models-catalog.json` | Models catalog | `application model catalog` | GET `/api/models/catalog` |
| `page-quality.json` | Quality | `data/m9/quality/quality_v1.json` | GET `/api/quality` |
| `page-run-create-now.json` | Run accepted for NOW | `synthetic, run id from events-t3-now` | POST `/api/live-runs` |
| `page-models-validate-ok.json` | Provider key validated | `synthetic` | POST `/api/models/validate` |
| `page-models-validate-rejected.json` | Provider key rejected | `synthetic, auth_failed is an existing provider validation error` | POST `/api/models/validate` |
| `page-run-cancel.json` | Run cancelled | `synthetic` | POST `/api/live-runs/{run_id}/cancel` |
| `page-quota-demo.json` | Five runs available | `synthetic` | GET `/api/quota` |
| `page-runs-history.json` | Six representative run history outcomes | `synthetic from page-a-* view fixtures` | GET `/api/live-runs` with `limit=10` |
| `page-workbench-options.json` | Workbench ticker and session options | `server /api/options 2026-11-21, subset AAPL MSFT NOW ORCL` | GET `/api/options` |
| `page-workbench-range-local.json` | Workbench price range | `server /api/range-local 2026-11-21` | GET `/api/range-local` |
| `page-workbench-ohlcv-aapl.json` | Workbench AAPL daily bars | `server /api/ohlcv/AAPL 2026-11-21` | GET `/api/ohlcv/{ticker}` (AAPL) |
| `page-workbench-ohlcv-msft.json` | Workbench MSFT daily bars | `server /api/ohlcv/MSFT 2026-11-21` | GET `/api/ohlcv/{ticker}` (MSFT) |
| `page-workbench-ohlcv-now.json` | Workbench NOW daily bars | `server /api/ohlcv/NOW 2026-11-21` | GET `/api/ohlcv/{ticker}` (NOW) |
| `page-workbench-ohlcv-orcl.json` | Workbench ORCL daily bars | `server /api/ohlcv/ORCL 2026-11-21` | GET `/api/ohlcv/{ticker}` (ORCL) |

## v5 abstention provenance

The `v5_r*m` runtime databases were queried through `GET /api/live-runs/{run_id}/view`. Those responses contain `legacy_text`, no `answer_snapshot`, and no structured abstention reason code. Structured abstention reason fixtures use actual API responses from `b3_smoke`, `b4_smoke2`, and `verify_source_smoke`; display-only possible-explanation and leads-only variants are marked synthetic. The v5 `PARTIAL` state is retained as a legacy view and is not represented as a typed abstention.
