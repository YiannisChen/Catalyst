import type React from 'react';
import { Link } from 'react-router';
import { Badge } from '@/components/ui';
import { formatEtDateTime } from '@/lib/format';
import { STEP_DEFS, dataStatus, isLatestJob, stageDone, stageTotal, type Job } from '@/features/data/model';

export type DataUpdatesSectionProps = {
  job?: Job;
  loading?: boolean;
  error?: boolean;
};

function et(value: string | null | undefined): string {
  if (!value) return '—';
  const formatted = formatEtDateTime(value);
  return formatted || '—';
}

/** A short status; the Data page holds the steps, sources and live progress. */
export function DataUpdatesSection({
  job,
  loading = false,
  error = false,
}: DataUpdatesSectionProps): React.JSX.Element {
  const latest = isLatestJob(job) ? job : undefined;
  const status = latest ? dataStatus(latest, false) : null;
  const stepsDone = latest
    ? STEP_DEFS.filter(({ key }) => {
        const total = stageTotal(latest, key);
        return total > 0 && stageDone(latest.stages[key] ?? {}) >= total;
      }).length
    : 0;

  return (
    <section
      id="data-updates"
      className="flex scroll-mt-6 flex-col rounded-lg border border-line-100 bg-bg-100 px-6 pt-5"
    >
      <div className="flex items-start justify-between gap-4 pb-4">
        <div className="flex flex-col gap-1">
          <h2 className="type-heading m-0">Data updates</h2>
          <p className="type-body-sm m-0 text-ink-200">
            The latest update in short. Steps, sources and live progress are on the Data page.
          </p>
        </div>
        {status && <Badge tone={status.tone}>{status.label}</Badge>}
      </div>
      {loading ? (
        <p className="border-t border-line-100 py-4 text-body-sm text-ink-300">Loading latest update…</p>
      ) : error ? (
        <p className="border-t border-line-100 py-4 text-body-sm text-ink-300">
          Could not load the latest update
        </p>
      ) : !latest ? (
        <p className="border-t border-line-100 py-4 text-body-sm text-ink-300">No update has run yet</p>
      ) : (
        <div className="grid gap-x-6 gap-y-3 border-t border-line-100 py-4 sm:grid-cols-3">
          <div className="flex flex-col gap-1">
            <span className="type-label text-ink-300">Started</span>
            <span className="font-numeric text-body-sm">{et(latest.started_at)}</span>
          </div>
          <div className="flex flex-col gap-1">
            <span className="type-label text-ink-300">Finished</span>
            <span className="font-numeric text-body-sm">
              {latest.finished_at ? et(latest.finished_at) : 'Not finished'}
            </span>
          </div>
          <div className="flex flex-col gap-1">
            <span className="type-label text-ink-300">Steps done</span>
            <span className="font-numeric text-body-sm">
              {stepsDone} of {STEP_DEFS.length}
            </span>
          </div>
        </div>
      )}
      <div className="border-t border-line-100 py-3">
        <Link to="/data" className="text-body-sm">
          Open the Data page
        </Link>
      </div>
    </section>
  );
}
