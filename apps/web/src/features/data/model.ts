import type { components } from '@/api/schema';

export type Job =
  components['schemas']['LatestJobResponse'] | components['schemas']['NoJobResponse'] | undefined;
export type LatestJob = components['schemas']['LatestJobResponse'];
export type Overview = components['schemas']['DataOverviewResponse'];

export const STEP_DEFS = [
  { key: 'prices', name: 'Prices', source: 'Polygon daily bars' },
  { key: 'sec', name: 'Filings', source: 'SEC EDGAR 8-K' },
  { key: 'movers', name: 'Movers', source: 'From prices' },
  { key: 'news', name: 'News', source: 'Finnhub archive, Polygon feed, publisher pages' },
  { key: 'verify', name: 'Checks', source: 'Coverage check per session' },
  { key: 'deferred', name: 'Retry', source: 'Pages that did not answer' },
] as const;

export function isLatestJob(job: Job): job is LatestJob {
  return Boolean(job && 'failures' in job);
}

export function fmtDate(value?: string | null, options: Intl.DateTimeFormatOptions = {}): string {
  if (!value) return '—';
  const date = new Date(value.length === 10 ? `${value}T12:00:00Z` : value);
  if (!Number.isFinite(date.getTime())) return '—';
  return new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    month: 'short',
    day: 'numeric',
    ...options,
  }).format(date);
}

export function fmtTime(value?: string | null): string {
  if (!value) return '—';
  return new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(value));
}

export function fmtBytes(bytes?: number | null): string {
  if (bytes == null) return '—';
  if (bytes >= 1_000_000_000) return `${(bytes / 1_000_000_000).toFixed(1)} GB`;
  if (bytes >= 1_000_000) return `${Math.round(bytes / 1_000_000)} MB`;
  if (bytes >= 1_000) return `${Math.max(1, Math.round(bytes / 1_000))} KB`;
  return `${bytes} B`;
}

export function stageCounts(job: LatestJob | undefined, key: string) {
  return job?.stages[key] ?? {};
}

export function stageDone(counts: Record<string, number>): number {
  return (counts.OK ?? 0) + (counts.EMPTY ?? 0) + (counts.FAILED ?? 0) + (counts.SKIPPED ?? 0);
}

export function stageTotal(job: LatestJob | undefined, key: string): number {
  const counts = stageCounts(job, key);
  const actual = Object.values(counts).reduce((sum, value) => sum + value, 0);
  return actual || job?.plan_summary?.items_to_do[key] || 0;
}

/** The step a stopped or running job is on: the first one with work left, in step order. */
export function activeStageKey(job: LatestJob | undefined, order: readonly string[]): string | undefined {
  if (!job) return undefined;
  if (job.current_stage) return job.current_stage;
  return order.find((key) => {
    const counts = stageCounts(job, key);
    return (counts.RUNNING ?? 0) + (counts.PENDING ?? 0) > 0;
  });
}

export function stageStatus(job: LatestJob | undefined, key: string, _index: number): string {
  const counts = stageCounts(job, key);
  const failed = counts.FAILED ?? 0;
  // A RUNNING item with no live updater is where the next run picks up.
  if (counts.RUNNING) return job?.running ? 'Running' : 'Resumes here';
  if (failed) return `Done with ${failed} failed`;
  if (counts.PENDING) return 'Waiting';
  if (stageDone(counts) > 0) return 'Done';
  if (job?.current_stage === key) return 'Resumes here';
  if (job?.status === 'COMPLETED' || job?.status === 'PARTIAL') return 'Saved';
  return 'Waiting';
}

/** 136 min -> "2 h 16 min"; 40 min -> "40 min". */
export function fmtDuration(seconds: number): string {
  const minutes = Math.max(1, Math.round(seconds / 60));
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} h ${rest} min` : `${hours} h`;
}

export function scrubError(value?: string | null): string {
  if (!value) return 'No detail provided';
  return value
    .replace(/^[A-Za-z_]*(Error|Exception): /, '')
    .replace(/[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}/g, '[email]')
    .replace(/(token|key|password|secret)[=: ]+[^\s,;]+/gi, '$1=[redacted]')
    .replace(/\s+/g, ' ')
    .slice(0, 180);
}

export function dataStatus(
  job: Job,
  stale: boolean,
): { label: string; tone: 'info' | 'warn' | 'up' | 'neutral' } {
  if (isLatestJob(job) && job.running) return { label: 'Updating', tone: 'info' };
  if (isLatestJob(job) && ['RUNNING', 'PENDING'].includes(job.status))
    return { label: 'Interrupted', tone: 'warn' };
  if (stale) return { label: 'Needs an update', tone: 'warn' };
  if (
    isLatestJob(job) &&
    job.status === 'COMPLETED' &&
    job.finished_at &&
    Date.now() - new Date(job.finished_at).getTime() <= 30 * 60 * 60_000
  )
    return { label: 'Up to date', tone: 'up' };
  if (isLatestJob(job) && job.status === 'PARTIAL') return { label: 'Partial', tone: 'warn' };
  return { label: 'Needs an update', tone: 'warn' };
}

export function storeDescription(name: string, overview: Overview, tickerCount: number) {
  const f = overview.freshness;
  const descriptions: Record<string, { group: string; label: string; holds: string; use: string }> = {
    market_db: {
      group: 'MARKET',
      label: 'Prices',
      holds: `Daily bars, ${f.price_symbols} symbols, ${fmtDate(f.prices_from, { year: 'numeric' })} – ${fmtDate(f.prices_through, { year: 'numeric' })}`,
      use: 'Move size, peers, sector, charts',
    },
    archive_db: {
      group: 'NEWS',
      label: 'Headline archive',
      holds: `${(f.headline_count ?? 0).toLocaleString()} Finnhub headlines, through ${fmtDate(f.headlines_through)}`,
      use: 'Which stories to read',
    },
    bodies_db: {
      group: 'NEWS',
      label: 'Article text',
      holds: 'Article bodies and page versions',
      use: 'Quotes in answers',
    },
    news_categories_db: {
      group: 'NEWS',
      label: 'News categories',
      holds: 'Topic labels per headline',
      use: 'Filtering the news list',
    },
    media_db: { group: 'NEWS', label: 'Images', holds: 'Headline thumbnails', use: 'News cards' },
    sec_submissions_dir: {
      group: 'FILINGS',
      label: 'SEC filings',
      holds: `8-K submissions per ${tickerCount} tickers`,
      use: 'Earnings timing, filing evidence',
    },
    web_search_db: {
      group: 'SEARCH',
      label: 'Web search cache',
      holds: 'Saved web results',
      use: 'Extra pages for runs',
    },
    jobs_db: { group: 'SYSTEM', label: 'Update jobs', holds: 'Every run, step and item', use: 'This page' },
    sec_earnings_cache: {
      group: 'SYSTEM',
      label: 'Earnings cache',
      holds: 'Earnings release times',
      use: 'Earnings timing',
    },
    corp_actions_db: {
      group: 'SYSTEM',
      label: 'Corporate actions',
      holds: 'Splits and ticker changes',
      use: 'Price adjustments',
    },
  };
  return (
    descriptions[name] ?? {
      group: 'SEARCH',
      label: name === 'Search index' || name === 'served_db' ? 'Search index' : 'Data store',
      holds:
        name === 'Search index' || name === 'served_db' ? 'Full-text index of all evidence' : 'Stored data',
      use: 'Every analysis run',
    }
  );
}
