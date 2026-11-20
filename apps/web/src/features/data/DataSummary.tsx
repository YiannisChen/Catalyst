import type { JSX } from 'react';
import type { components } from '@/api/schema';
import { Stat } from '@/components/catalyst/Stat';
import { Badge } from '@/components/ui';
import { formatBytes } from './format';

type Overview = components['schemas']['DataOverviewResponse'];
type Job = components['schemas']['LatestJobResponse'] | components['schemas']['NoJobResponse'] | undefined;

export function DataSummary({
  overview,
  job,
  tickerCount,
}: {
  overview: Overview;
  job: Job;
  tickerCount: number;
}): JSX.Element {
  const latest = job && 'failures' in job ? job : undefined;
  const label = latest
    ? ({
        COMPLETED: 'Complete',
        COMPLETE: 'Complete',
        PARTIAL: 'Partial',
        RUNNING: 'Running',
        STARTED: 'Running',
        FAILED: 'Failed',
      }[latest.status.toUpperCase()] ?? 'Running')
    : 'No update yet';
  // A RUNNING job whose updater is gone stopped before it finished.
  const shown = label === 'Running' && latest && latest.running === false ? 'Interrupted' : label;
  const tone =
    shown === 'Complete' ? 'up' : shown === 'Failed' ? 'down' : shown === 'Running' ? 'info' : 'warn';
  return (
    <div className="grid grid-cols-2 gap-2 xl:grid-cols-5">
      <div className="flex min-w-0 flex-col gap-2 rounded-lg border border-line-100 bg-bg-100 p-4">
        <Stat
          label="Sessions"
          value={overview.coverage.length || (latest?.plan.sessions as number | undefined) || '—'}
          size="sm"
        />
      </div>
      <div className="flex min-w-0 flex-col gap-2 rounded-lg border border-line-100 bg-bg-100 p-4">
        <Stat label="Tickers" value={tickerCount} size="sm" />
      </div>
      <div className="flex min-w-0 flex-col gap-2 rounded-lg border border-line-100 bg-bg-100 p-4">
        <Stat
          label="Latest update"
          value={latest ? <Badge tone={tone}>{shown}</Badge> : <Badge>No update yet</Badge>}
          size="sm"
        />
        <span className="type-caption text-ink-300">
          {latest ? `${latest.failures.length} failed items` : ' '}
        </span>
      </div>
      <div className="flex min-w-0 flex-col gap-2 rounded-lg border border-line-100 bg-bg-100 p-4">
        <Stat label="Search index" value={overview.search_index.built ? 'Built' : 'Not built'} size="sm" />
      </div>
      {overview.disk_free_bytes != null && (
        <div className="flex min-w-0 flex-col gap-2 rounded-lg border border-line-100 bg-bg-100 p-4">
          <Stat label="Disk" value={`${formatBytes(overview.disk_free_bytes)} free`} size="sm" />
        </div>
      )}
    </div>
  );
}
