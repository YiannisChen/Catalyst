import type { JSX } from 'react';
import { Skeleton } from '@/components/ui';
import { fmtBytes, fmtDate, isLatestJob, stageCounts, stageDone } from '@/features/data/model';
import type { Job, Overview } from '@/features/data/model';
function Tile({
  label,
  value,
  caption,
  stale = false,
}: {
  label: string;
  value: string;
  caption: string;
  stale?: boolean;
}): JSX.Element {
  return (
    <div className={`data-freshness-tile ${stale ? 'polish-data-tile-stale' : ''}`}>
      <span className="type-label text-ink-300">{label}</span>
      <span className="text-body font-medium">{value}</span>
      <span className={`type-caption ${stale ? 'text-warn' : 'text-ink-300'}`}>{caption}</span>
    </div>
  );
}

export function Freshness({
  overview,
  job,
  tickerCount,
}: {
  overview?: Overview;
  job: Job;
  tickerCount: number;
}): JSX.Element {
  if (!overview)
    return (
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }, (_, i) => (
          <div key={i} className="data-freshness-tile">
            <Skeleton width="45%" />
            <Skeleton height={20} />
            <Skeleton width="65%" />
          </div>
        ))}
      </div>
    );
  const freshness = overview.freshness;
  const latestCoverage = overview.coverage[0]?.session_date;
  const pricesCurrent = Boolean(freshness.prices_through && freshness.prices_through === latestCoverage);
  const priceBehind = freshness.prices_through
    ? overview.coverage.filter((session) => session.session_date > freshness.prices_through!).length
    : overview.coverage.length;
  const headlineThrough = freshness.headlines_through;
  const headlineBehind = headlineThrough
    ? overview.coverage.filter((s) => s.session_date > headlineThrough && !s.tickers_with_news.length).length
    : overview.coverage.length;
  const secItem = isLatestJob(job)
    ? job.recent_items.find((item) => item.stage === 'sec' && item.finished_at)
    : undefined;
  const secStore = overview.stores.find((store) => store.path_kind === 'sec_submissions_dir');
  const secCounts = isLatestJob(job) ? stageCounts(job, 'sec') : {};
  const secDone = stageDone(secCounts);
  const secRound =
    isLatestJob(job) && !job.finished_at
      ? `${secDone} of ${tickerCount || job.plan_summary?.universe_size || 0} tickers this round`
      : `${secDone} checked this round`;
  const search = overview.search_index;
  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-4">
      <Tile
        label="Prices"
        value={`through ${fmtDate(freshness.prices_through)}`}
        caption={
          pricesCurrent ? `Current · ${freshness.price_symbols} symbols` : `${priceBehind} sessions behind`
        }
        stale={!pricesCurrent}
      />
      <Tile
        label="Company headlines"
        value={`through ${fmtDate(headlineThrough)}`}
        caption={
          headlineBehind ? `${headlineBehind} sessions behind` : `${freshness.headline_count ?? 0} headlines`
        }
        stale={headlineBehind > 0}
      />
      <Tile
        label="Filings (8-K)"
        value={`checked ${fmtDate(secItem?.finished_at ?? secStore?.modified_at)}`}
        caption={secRound}
      />
      <Tile
        label="Search index"
        value={`${search.built ? 'built' : 'not built'} · ${fmtBytes(search.bytes)}`}
        caption="What analyses search"
      />
    </div>
  );
}
