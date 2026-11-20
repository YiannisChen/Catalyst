import type { JSX } from 'react';
import type { components } from '@/api/schema';
import { Panel } from '@/components/ui';
import {
  activeStageKey,
  fmtDuration,
  fmtDate,
  fmtTime,
  stageCounts,
  stageDone,
  stageStatus,
  stageTotal,
  STEP_DEFS,
} from '@/features/data/model';
import type { LatestJob, Overview } from '@/features/data/model';
import { time, stageWork } from './dataPage';
import { Attention } from './Attention';
import { RunChart } from './RunChart';
import { Sources } from './Sources';
export function IdleUpdate({
  overview,
  job,
  history,
  schedule,
  actions,
}: {
  overview: Overview;
  job: LatestJob | undefined;
  history: components['schemas']['DataJobHistoryItem'][];
  schedule?: components['schemas']['DataScheduleResponse'];
  actions: JSX.Element;
}): JSX.Element {
  const interrupted = Boolean(job && ['RUNNING', 'PENDING'].includes(job.status) && !job.running);
  const activeKey = activeStageKey(
    job,
    STEP_DEFS.map((s) => s.key),
  );
  const step = STEP_DEFS.find((s) => s.key === activeKey);
  const stepDone = step ? stageDone(stageCounts(job, step.key)) : 0;
  const stepTotal = step ? stageTotal(job, step.key) : 0;
  const estimate = job?.plan_summary?.eta_seconds;
  const low = interrupted ? job?.remaining_seconds : estimate?.[0];
  const high = interrupted ? job?.remaining_seconds : estimate?.[1];
  const estimateText =
    low == null
      ? 'estimate unavailable'
      : interrupted
        ? `about ${fmtDuration(low)} remaining`
        : `${(low / 3600).toFixed(1)} – ${(high ?? low) / 3600 < 10 ? ((high ?? low) / 3600).toFixed(1) : Math.round((high ?? low) / 3600)} h`;
  const first = job?.plan_summary?.first_session;
  const last = job?.plan_summary?.last_session;
  return (
    <Panel
      className="h-auto shrink-0"
      flush
      title={
        <div className="polish-data-update-title">
          <h2 className="type-heading m-0">Update</h2>
          <span className="type-caption text-ink-300">
            Every weekday at 6:00 AM ET · next{' '}
            {schedule
              ? time(schedule.next_run_at, { weekday: 'short', month: 'short', day: 'numeric' })
              : '—'}
          </span>
        </div>
      }
      actions={[<span key="update-actions">{actions}</span>]}
      bodyClassName="gap-0 p-0"
    >
      <div className="polish-data-update-columns">
        <div className="flex min-w-0 flex-col gap-4 p-4">
          <div className="flex flex-col gap-1">
            <span className="type-label text-ink-300">
              Last run · started {job?.started_at ? fmtTime(job.started_at) : '—'}
            </span>
            <p className="type-body-sm m-0 text-ink-200">
              {interrupted && step
                ? `Stopped during ${step.name} (${stepDone} of ${stepTotal}), with earlier steps saved; resuming continues there without fetching items twice.`
                : job?.status === 'COMPLETED'
                  ? `Completed ${fmtTime(job.finished_at)}. Saved prices, filings, news and coverage checks for this run.`
                  : job
                    ? `Last run ${job.status.toLowerCase()}${job.finished_at ? ` at ${fmtTime(job.finished_at)}` : ''}. Resuming continues from the saved work.`
                    : 'No update has run yet.'}
            </p>
          </div>
          <Attention overview={overview} job={job} />
          <RunChart items={history} />
        </div>
        <div className="flex min-w-0 flex-col gap-4 border-line-100 p-4 polish-data-update-right">
          <div className="data-next-run">
            <span className="type-label text-ink-300">
              Next run · sessions {fmtDate(first)} – {fmtDate(last)}
            </span>
            <span className="text-caption text-ink-200">estimate {estimateText}</span>
          </div>
          <div className="overflow-x-auto">
            <table className="data-steps-table cx-table cx-table-compact w-full">
              <thead>
                <tr>
                  <th>Step</th>
                  <th>Source</th>
                  <th>Work</th>
                  <th className="text-right">Status</th>
                </tr>
              </thead>
              <tbody>
                {STEP_DEFS.map((def, index) => (
                  <tr key={def.key}>
                    <td className="font-medium whitespace-nowrap">
                      <span className="font-numeric text-ink-300">{index + 1}</span> {def.name}
                    </td>
                    <td className="whitespace-nowrap text-ink-200">
                      {def.key === 'movers'
                        ? `From prices (|move| ≥ ${job?.plan_summary?.mover_threshold_pct ?? 2}%)`
                        : def.source}
                    </td>
                    <td className="whitespace-nowrap text-body-sm text-ink-200">{stageWork(job, def.key)}</td>
                    <td className="whitespace-nowrap text-right text-caption text-ink-200">
                      {stageStatus(job, def.key, index)
                        .replace(/Done with (\d+) failed/, 'Done · $1 failed')
                        .replace('Saved', 'Done')}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Sources overview={overview} job={job} running={false} />
        </div>
      </div>
    </Panel>
  );
}
