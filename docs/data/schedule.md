# Scheduled Jobs

> Status checked 2026-11-21. The server archive collectors listed below were retired on 2026-11-12. The local prospective-evaluation launchd jobs were unloaded on 2026-11-21; the evaluation ended 2026-11-13.

## 1. Jobs Running Now

| Host | Job | Schedule (UTC) |
|---|---|---|
| US server | `catalyst-health` | Every 10 minutes |
| US server | `catalyst-collect-alpaca-news` | Hourly |
| US server | `catalyst-headline-archive` | Daily at 10:30 |
| US server | `catalyst-daily` | Tue–Sat at 11:00 |
| US server | `catalyst-backup` | Daily at 15:00 |
| Hong Kong | `catalyst-monitor-check` | Every 15 minutes |
| Hong Kong | `catalyst-monitor-daily` daily report | Daily at 14:30 |
| Hong Kong | `catalyst-monitor-pull-backup` | Daily at 16:00 |
| Hong Kong | `catalyst-monitor-restore-drill` | 2nd day of each month at 17:00 |

The following Mac launchd jobs are unloaded; their plists are retained in `.local/archive/launchd/`: `com.catalyst.prospective`, `com.catalyst.prospective-check`, `com.catalyst.prospective2`, `com.catalyst.prospective2-check`, `com.catalyst.gold`, `com.catalyst.tavily-check`, and `com.catalyst.sec-snapshot`.

## 2. Retired 2026-11-12

Archive collector timer groups: `calendar`, `finra-shvol`, `market-events`, `market-minute`, `market-options`, `market-ratings`, `news-company`, `news-fast`, `news-gdelt`, `news-hourly`, `news-marketaux`, `press`, `regulatory-fda`, `regulatory-fr`, and `trial-times`. Their SQLite stores remain frozen under `/opt/catalyst/archive/` and are not read by attribution.

## 3. Quota Sharing

The collectors' `SharedBucket` shares quotas only among server processes using the same state directory. `catalyst-daily` uses an in-process rate limiter and does not share that bucket. Local collector ceilings are not provider quota allocations:

- Finnhub: 50 requests/minute
- Polygon: 4 requests/minute
- Alpaca: 150 requests/minute

For bulk backfills or research fetches, run one task per provider at a time, use a conservative budget when remaining quota is unknown, and back off on 429 responses. Record limits, current usage, and remaining quota (including unknown values) before starting. Do not record credentials.
