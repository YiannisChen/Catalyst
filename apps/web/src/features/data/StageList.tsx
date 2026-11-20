import type { JSX } from 'react';
import type { components } from '@/api/schema';
import { Badge } from '@/components/ui';
import { formatEtDateTime } from '@/lib/format';

type Job = components['schemas']['LatestJobResponse'];
const labels: Record<string, string> = {
  prices: 'Prices',
  news: 'News',
  sec: 'Filings',
  movers: 'Movers',
  verify: 'Checks',
};
function titleCase(value: string): string {
  return value.replace(/[_-]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}
function date(value: string | null): string {
  return value ? formatEtDateTime(value) : 'In progress';
}
function duration(job: Job): string {
  if (!job.started_at || !job.finished_at) return '';
  const minutes = (new Date(job.finished_at).getTime() - new Date(job.started_at).getTime()) / 60_000;
  return `${minutes.toFixed(1)} min`;
}
export function StageList({ job }: { job?: Job }): JSX.Element {
  const stages = job ? Object.entries(job.stages) : [];
  return (
    <section className="flex min-w-0 shrink-0 flex-col overflow-hidden rounded-lg border border-line-100 bg-bg-100">
      <header className="panel-header flex items-center justify-between gap-2 border-b border-line-100 px-4">
        <h2 className="type-heading m-0">Last update by stage</h2>
        <span className="text-caption text-ink-300">
          {job ? (
            <>
              {date(job.finished_at)}
              {duration(job) ? (
                <>
                  {' '}
                  · <span className="font-numeric">{duration(job)}</span>
                </>
              ) : null}
            </>
          ) : (
            'No update yet'
          )}
        </span>
      </header>
      {stages.length ? (
        <div className="flex flex-col divide-y divide-line-100 px-4">
          {stages.map(([key, counts]) => {
            const entries = Object.entries(counts);
            const failed = counts.FAILED ?? 0;
            const tone = failed
              ? 'down'
              : (counts.PENDING ?? 0) > 0
                ? 'info'
                : (counts.SKIPPED ?? 0) > 0
                  ? 'warn'
                  : 'up';
            const status = failed
              ? 'Failed'
              : (counts.PENDING ?? 0) > 0
                ? 'Running'
                : (counts.OK ?? 0) > 0
                  ? 'Complete'
                  : (counts.SKIPPED ?? 0) > 0
                    ? 'Skipped'
                    : 'Empty';
            return (
              <div key={key} className="flex flex-wrap items-center justify-between gap-3 py-3">
                <span className="text-body-sm font-medium">{labels[key] ?? titleCase(key)}</span>
                <span className="flex flex-wrap items-center gap-3">
                  <Badge tone={tone}>{status}</Badge>
                  <span className="font-numeric text-caption text-ink-200">
                    {entries
                      .map(
                        ([name, count]) =>
                          `${({ OK: 'Complete', EMPTY: 'Empty', SKIPPED: 'Skipped', FAILED: 'Failed', PENDING: 'Pending' } as Record<string, string>)[name] ?? titleCase(name)} ${count}`,
                      )
                      .join(' · ')}
                  </span>
                </span>
              </div>
            );
          })}
        </div>
      ) : (
        <p className="type-body-sm m-0 px-4 py-4 text-ink-300">No stage details available.</p>
      )}
    </section>
  );
}
