import type { JSX } from 'react';
import { fmtDate, fmtTime } from '@/features/data/model';
import type { LatestJob, Overview } from '@/features/data/model';
export function Sources({
  overview,
  job,
  running,
}: {
  overview: Overview;
  job: LatestJob | undefined;
  running: boolean;
}): JSX.Element {
  const store = (key: string) => overview.stores.find((item) => item.path_kind === key);
  const hosts = Object.entries(job?.deferred_by_host ?? {})
    .filter(([, counts]) => (counts.PENDING ?? 0) > 0)
    .map(([host]) => host);
  const relayFailure = Boolean(
    job?.failures.some((f) => /relay/i.test(f.error ?? '')) ||
    job?.recent_items.some((i) => /relay/i.test(i.error ?? '')),
  );
  const sources = [
    {
      name: 'Polygon',
      gives: running ? 'Prices + news feed' : 'Daily prices; news feed',
      state: store('market_db')?.modified_at
        ? `last good ${fmtTime(store('market_db')?.modified_at)}`
        : 'no saved data',
      warn: false,
    },
    {
      name: 'SEC EDGAR',
      gives: '8-K filings and exhibits',
      state: `${fmtTime(store('sec_submissions_dir')?.modified_at)}${job?.failures.filter((f) => f.stage === 'sec').length ? ` · ${job.failures.filter((f) => f.stage === 'sec').length} failed` : ''}`,
      warn: false,
    },
    {
      name: 'Finnhub archive',
      gives: 'Company headlines',
      state: `latest headline ${fmtDate(overview.freshness.headlines_through)}`,
      warn:
        !overview.freshness.headlines_through ||
        overview.coverage.some(
          (s) => overview.freshness.headlines_through! < s.session_date && !s.tickers_with_news.length,
        ),
    },
    {
      name: 'Publisher pages',
      gives: 'Article text',
      state: hosts.length
        ? `${hosts.length} host${hosts.length === 1 ? '' : 's'} waiting · ${hosts.join(', ')}`
        : 'no deferred pages waiting',
      warn: hosts.length > 0,
    },
    {
      name: 'Benzinga / Kitco',
      gives: 'Story discovery via relay',
      state: relayFailure ? 'last error: relay unreachable' : 'available for discovery',
      warn: relayFailure,
    },
    {
      name: 'Web search',
      gives: 'Extra pages for runs (cached)',
      state: store('web_search_db')?.modified_at
        ? `cache updated ${fmtTime(store('web_search_db')?.modified_at)}`
        : 'cache not created',
      warn: false,
    },
  ];
  return (
    <div className="flex flex-col gap-2">
      <span className="type-label text-ink-300">{running ? 'Sources this run' : 'Sources'}</span>
      <div className="polish-data-source-list">
        {sources.map((source) => (
          <div key={source.name} className="polish-data-source-row">
            <span className={`data-source-dot ${source.warn ? 'bg-warn' : 'bg-up'}`} />
            <span className="text-body-sm font-semibold">{source.name}</span>
            <span className="type-caption text-ink-200">{source.gives}</span>
            <span className={`text-code ${source.warn ? 'text-warn' : 'text-ink-300'}`}>{source.state}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
