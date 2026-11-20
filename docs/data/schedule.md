# Scheduled Jobs and Daylight Saving Time

| Item | Details |
|---|---|
| Date | 2026-10-23 |
| Rule | Always write Pacific Time (PT) first, followed by UTC in parentheses. Schedule jobs on servers and monitoring hosts in UTC; schedule Mac `launchd` jobs in local time (PT); schedule jobs tied to the U.S. market close in `America/New_York` (`systemd` 255 supports this), so they automatically follow daylight saving time changes. |
| Daylight saving time | Ends on 2026-11-01 (Sunday): PDT (UTC−7) → PST (UTC−8); New York EDT (UTC−4) → EST (UTC−5). “Current” in the table below means before 11-01; “From 11-01” means after that date. |
| Two restricted windows | <ul><li>**Evaluation window**: Tue–Sat 06:30–09:30 PT. Currently this is 13:30–16:30 UTC; from 11-01 it is 14:30–17:30 UTC;</li><li>**Daily update window**: Tue–Sat 11:00–13:30 UTC. Currently this is 04:00–06:30 PT; from 11-01 it is 03:00–05:30 PT.</li></ul>Do not schedule bulk network tasks (backfills, research fetches) during either window. Always-on, low-volume collectors (once every 5–60 minutes) run all day. Section 3 defines quota sharing precisely: sharing applies only to server processes using the same state directory; evaluation runs on the Mac are separate. |

## 1. All Scheduled Jobs

| Host | Job | Schedule | Current (PT / UTC) | From 11-01 (PT / UTC) | In a restricted window? |
|---|---|---|---|---|---|
| US | `catalyst-daily` daily update | Tue–Sat 11:00 UTC | 04:00 / 11:00 | 03:00 / 11:00 | It is the daily update window |
| US | `catalyst-backup` nightly backup | Daily at 15:00 UTC | 08:00 / 15:00 | 07:00 / 15:00 | **During the evaluation window**; performs local reads and writes only, with no network access and no provider quota use |
| US | `catalyst-health` health report | Every 10 minutes | — | — | Reads local data only, no network access |
| US | `collect-press` press release RSS | Every 5 minutes | — | — | Always-on, low-volume |
| US | `collect-news-fast` Finnhub market news | Every 15 minutes | — | — | Always-on, low-volume (1 request per run) |
| US | `collect-alpaca-news` | Hourly at :10 | — | — | Always-on, low-volume |
| US | `collect-news-hourly` Seeking Alpha, NewsAPI | Hourly at :20 | — | — | Always-on, low-volume |
| US | `collect-news-gdelt` | Hourly at :50 | — | — | Always-on, low-volume |
| US | `collect-news-marketaux` | Every 2 hours at :40 (even UTC hours) | — | — | Always-on, low-volume |
| US | `collect-news-company` Finnhub company news (about 101 requests per run) | 00:30, 06:30, 18:30, 22:30 UTC | 17:30, 23:30, 11:30, 15:30 | 16:30, 22:30, 10:30, 14:30 | No: 06:30 UTC and 18:30 UTC are outside both windows, and remain so from 11-01 |
| US | `collect-calendar` earnings calendar | 10:15, 22:15 UTC (from 10-22; previously 12:15 UTC, which fell in the daily update window) | 03:15, 15:15 | 02:15, 14:15 | No |
| US | `collect-market-minute` minute bars | Tue–Sat 01:30 UTC | 18:30 (previous day) | 17:30 (previous day) | No |
| US | `collect-market-options` options snapshot | Mon–Fri 16:30 **America/New_York** (from 10-22; previously 20:30 UTC, which would become 15:30 New York time from 11-01, before market close) | 13:30 / 20:30 | 13:30 / 21:30 | No |
| US | `collect-market-ratings` ratings | Daily at 02:30 UTC | 19:30 (previous day) | 18:30 (previous day) | No |
| US | `collect-market-events` events calendar | Daily at 03:00 UTC | 20:00 (previous day) | 19:00 (previous day) | No |
| US | `collect-finra-shvol` | Tue–Sat 04:00 UTC | 21:00 (previous day) | 20:00 (previous day) | No |
| US | `collect-regulatory-fr`, `-fda` | 04:15, 04:30 UTC | 21:15, 21:30 (previous day) | 20:15, 20:30 (previous day) | No |
| US | `collect-trial-times` trial-run sampling | Daily at 05:00 UTC | 22:00 (previous day) | 21:00 (previous day) | No |
| Hong Kong | `catalyst-monitor-check` | Every 15 minutes | — | — | Reads health reports through the gate only |
| Hong Kong | `catalyst-monitor-daily` daily report | Daily at 14:30 UTC | 07:30 / 14:30 | 06:30 / 14:30 | During the evaluation window; reads health reports and sends one email only |
| Hong Kong | `catalyst-monitor-pull-backup` | Daily at 16:00 UTC | 09:00 / 16:00 | 08:00 / 16:00 | During the evaluation window; transfers files between the two owned hosts only, with no provider access |
| Hong Kong | `catalyst-monitor-restore-drill` | 2nd day of each month at 17:00 UTC | 10:00 / 17:00 | 09:00 / 17:00 | From 11-01, falls in the evaluation window if the 2nd is Tue–Sat (12-02 is Wednesday); reads the local Hong Kong host only. **Recommendation**: it will not run again before the evaluation ends (the next run is Monday 11-02 at 09:00 PST); move it to 19:00 UTC starting in December |
| Mac | `com.catalyst.prospective` first run | Tue–Sat 07:00 PT | 07:00 / 14:00 | 07:00 / 15:00 | The evaluation itself |
| Mac | `com.catalyst.prospective2` second run | Tue–Sat 07:10 PT (wait 25 minutes after the first run is submitted) | 07:10 / 14:10 | 07:10 / 15:10 | The evaluation itself |
| Mac | `prospective-check`, `prospective2-check` | Tue–Sat 08:30, 08:50 PT | — | — | The evaluation itself |
| Mac | `tavily-check` | Tue–Sat 09:10 PT | 09:10 / 16:10 | 09:10 / 17:10 | The evaluation itself |
| Mac | `com.catalyst.gold` gold labels | Daily at 10:00 PT | 10:00 / 17:00 | 10:00 / 18:00 | No (after the evaluation window) |
| Mac | Benzinga relay and reverse tunnel | Always on | — | — | Supports the daily update (11:00 UTC) |

**Summary**:
- No bulk network tasks run during the evaluation or daily update windows.
- Two schedules have changed: the earnings calendar previously ran during the daily update window; the options snapshot would run before market close after 11-01.
- Only the local backup, monitoring daily report, backup pull, and always-on low-volume collectors run during the evaluation window. None runs bulk requests.

## 2. Scheduling One-Time Tasks

- **Time**: Write PT followed by UTC, for example, “10-22 11:20 PT (18:20 UTC).” Check against the two restricted windows above and convert using the daylight saving time status on the scheduled date.
- **Runtime limit**: When using `systemd-run`, also set `RuntimeMaxSec` so the task stops before the next restricted window begins:
  - Current: stop by 11:00 UTC (04:00 PT) at the latest to avoid the daily update window;
  - From 11-01: the daily update window still begins at 11:00 UTC (03:00 PT), and the evaluation window now begins at 14:30 UTC.
- **Dependencies**: To run the next step after the previous step completes, use completion marker files rather than the transient systemd unit's status. A transient unit is unloaded after it finishes, so its status is unavailable; this prevented the next step from starting in the 10-22 backfill:
  - Run the previous step with `/opt/catalyst/bin/catalyst-job NAME command...`. It writes `/opt/catalyst/jobs/NAME.started`, then writes `NAME.done` (exit code 0) or `NAME.failed` (including if it was terminated);
  - Run the next step with `catalyst-job --wait NAME [maximum seconds]` to wait for it.
- **Current one-time task**: `catalyst-research-secfix`, an SEC receipt-time reference table, starts at 10-22 11:15 PT (18:15 UTC), takes about 3 hours, and has a runtime limit of about 14 hours. Check the output file's line count to determine whether it completed.

## 3. Quota Sharing and Bulk Task Usage

**Exact scope of the shared bucket**
- The collectors' `SharedBucket` shares quotas only among server processes using the **same state directory**. Current behavior:
  - The daily update job (`catalyst-daily`) currently uses an in-process rate limiter and does not share the collectors' `SharedBucket`;
  - The prospective evaluation runner on the Mac is a **separate process on a separate host** and does not coordinate with any server bucket.
- Therefore, the collectors' rate limiter does **not** “reserve remaining quota” for the Mac or the daily update.

**Local limits for collectors on the server** (these are local ceilings, not known allocations of the providers' total quotas):
- Finnhub: 50 requests/minute;
- Polygon: 4 requests/minute;
- Alpaca: 150 requests/minute.

**Usage rules for bulk tasks (backfills, research fetches)**
1. Bulk tasks must avoid the evaluation and daily update windows. Run only one bulk task per provider at a time.
2. Set the bulk budget to `min(local budget, provider remaining quota − reserve)`. **The reserve value has not been set**; it must be determined from actual provider usage data. Do not make up a value.
3. If the provider's remaining quota is unknown, use a conservative budget and apply backoff on 429 responses. **There is no hard guarantee across hosts**: the server and Mac operate independently.
4. Before each bulk task starts, record the limit, current usage, and remaining quota, including whether each value is known. **Do not record credentials.**
5. **Scope**: Interactive coding (small requests while a person is present) is not currently restricted by these windows. The windows and budget rules in this section apply only to bulk network tasks.
