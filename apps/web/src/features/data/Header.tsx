import type { JSX } from 'react';
import { Badge } from '@/components/ui';
import { dataStatus, fmtDate } from '@/features/data/model';
import type { Job, Overview } from '@/features/data/model';
export function Header({
  job,
  stale,
  overview,
}: {
  job: Job;
  stale: boolean;
  overview?: Overview;
}): JSX.Element {
  const state = dataStatus(job, stale);
  const prices = overview?.freshness.prices_through;
  const pricesCurrent = Boolean(prices && prices === overview?.coverage[0]?.session_date);
  const headlines = overview?.freshness.headlines_through;
  const headlineStale = Boolean(
    overview &&
    (!headlines || overview.coverage.some((s) => headlines < s.session_date && !s.tickers_with_news.length)),
  );
  return (
    <section className="data-page-header">
      <div className="data-page-heading">
        <div className="flex flex-col gap-1">
          <h1 className="type-display m-0">Data center</h1>
          <p className="type-body m-0 text-ink-100">
            {overview ? (
              <>
                {prices
                  ? pricesCurrent
                    ? `Prices are current through ${fmtDate(prices)}.`
                    : `Prices were last updated through ${fmtDate(prices)}.`
                  : 'Prices have no recorded update date.'}{' '}
                {headlineStale ? (
                  <>
                    <span className="text-warn">
                      {headlines
                        ? `Company headlines stop at ${fmtDate(headlines)}`
                        : 'Company headlines have no recorded update date'}
                    </span>
                    , so analyses of later days have no headlines to read.
                  </>
                ) : (
                  'Company headlines are current.'
                )}
              </>
            ) : (
              'Checking data freshness…'
            )}
          </p>
          <span className="type-caption text-ink-300">Times are New York time (ET).</span>
        </div>
        <Badge className="shrink-0" tone={state.tone} dot={state.label === 'Updating'}>
          {state.label}
        </Badge>
      </div>
    </section>
  );
}
