import type { JSX } from 'react';
import { Panel } from '@/components/ui';
import {
  fmtDuration,
  fmtDate,
  fmtTime,
  stageCounts,
  stageDone,
  stageTotal,
  STEP_DEFS,
} from '@/features/data/model';
import type { LatestJob, Overview } from '@/features/data/model';
import { pct, time } from './dataPage';
import { Sources } from './Sources';
export function RunningUpdate({
  overview,
  job,
  actions,
}: {
  overview: Overview;
  job: LatestJob;
  actions: JSX.Element;
}): JSX.Element {
  const currentIndex = Math.max(
    0,
    STEP_DEFS.findIndex((step) => step.key === (job.current_stage ?? job.current_item?.stage)),
  );
  const currentStep = STEP_DEFS[currentIndex];
  const plannedTotal = Object.values(job.plan_summary?.items_to_do ?? {}).reduce(
    (sum, count) => sum + count,
    0,
  );
  const total = Math.max(job.items_total, plannedTotal);
  const failed = Object.values(job.stages).reduce((sum, counts) => sum + (counts.FAILED ?? 0), 0);
  // Progress by time, not item count: one news story takes minutes, one price a second.
  const remaining = job.remaining_seconds ?? 0;
  const doneWidth =
    job.active_seconds + remaining > 0
      ? pct(job.active_seconds, job.active_seconds + remaining)
      : pct(job.items_done, total);
  const failWidth = pct(failed, total);
  const steps = STEP_DEFS.map((def, index) => {
    const counts = stageCounts(job, def.key);
    const count = stageTotal(job, def.key);
    const done = stageDone(counts);
    const active = index === currentIndex;
    const waiting = !done && !counts.RUNNING && index > currentIndex;
    return {
      ...def,
      index,
      counts,
      count,
      done,
      active,
      waiting,
      status: counts.RUNNING
        ? 'Running'
        : waiting
          ? 'Waiting'
          : count && done >= count
            ? 'Done'
            : failed && done
              ? `Done · ${counts.FAILED} failed`
              : 'Waiting',
    };
  });
  const resultText = (stage: string, status: string) =>
    status === 'OK'
      ? stage === 'sec'
        ? 'New 8-K found'
        : 'Saved'
      : status === 'EMPTY'
        ? stage === 'sec'
          ? 'No new 8-K'
          : 'Nothing new'
        : status === 'FAILED'
          ? 'Failed · retry later'
          : status;
  return (
    <Panel
      className="h-auto shrink-0"
      flush
      title={
        <div className="polish-data-running-title">
          <h2 className="type-heading m-0">1 · Update</h2>
          <span className="type-caption text-ink-300">
            Sessions {fmtDate(job.plan_summary?.first_session)} – {fmtDate(job.plan_summary?.last_session)} ·
            started {fmtTime(job.started_at)} ET · runs on the{' '}
            {job.runs_on === 'server' ? 'server' : 'admin machine'}
          </span>
        </div>
      }
      actions={[<span key="update-actions">{actions}</span>]}
      bodyClassName="gap-4 p-4"
    >
      <div className="flex flex-col gap-2">
        <div className="polish-data-running-heading">
          <span className="type-title">
            Step {currentIndex + 1} of 6 · {currentStep?.name ?? 'Starting'} · now{' '}
            <span>{job.current_item?.item ?? 'working'}</span>
          </span>
          <span className="text-body-sm text-ink-200">
            <span className="font-numeric">{fmtDuration(job.active_seconds)}</span> working · about{' '}
            <span className="font-numeric">{fmtDuration(job.remaining_seconds ?? 0)}</span> left
          </span>
        </div>
        <div className="data-progress">
          <span className="bg-up" style={{ width: `${doneWidth}%` }} />
          <span className="bg-warn" style={{ width: `${failWidth}%` }} />
        </div>
        <div className="flex flex-wrap justify-between gap-2">
          <span className="type-caption text-ink-300">
            {job.items_done} of {total} planned items done · {failed} failed, will retry
          </span>
          <span className="type-caption text-ink-300">
            Estimate updates as the run goes: news stories are known only after Movers
          </span>
        </div>
      </div>
      <ol className="data-step-cards">
        {steps.map((step) => (
          <li key={step.key} className={`data-step-card ${step.active ? 'data-step-active' : ''}`}>
            <div className="flex items-center justify-between gap-2">
              <span className="text-body-sm font-semibold">
                {step.index + 1} · {step.name}
              </span>
              <span
                className={`type-caption ${step.active ? 'text-info' : step.waiting ? 'text-ink-300' : 'text-up'}`}
              >
                {step.status}
              </span>
            </div>
            <div className="data-progress data-progress-small">
              <span
                className={step.active ? 'bg-info' : 'bg-up'}
                style={{ width: `${pct(step.done, step.count)}%` }}
              />
            </div>
            <span className="text-code text-ink-200">
              {step.count ? (
                <>
                  <span className="font-numeric">
                    {step.done} / {step.count}
                  </span>
                  {step.counts.FAILED ? (
                    <>
                      {' '}
                      · <span className="font-numeric">{step.counts.FAILED}</span> failed
                    </>
                  ) : null}
                </>
              ) : (
                <>
                  ≈<span className="font-numeric">{job.plan_summary?.items_to_do[step.key] ?? 0}</span>
                </>
              )}
            </span>
            <span className="polish-data-step-source type-caption text-ink-300">{step.source}</span>
          </li>
        ))}
      </ol>
      <div className="data-running-columns">
        <div className="min-w-0">
          <span className="type-label text-ink-300">Live activity · refreshes every 5 s</span>
          <div className="mt-2 overflow-x-auto">
            <table className="cx-table cx-table-compact w-full">
              <tbody>
                {job.recent_items.map((item, index) => (
                  <tr key={`${item.stage}-${item.item}-${item.started_at ?? index}`}>
                    <td className="text-caption text-ink-300">
                      {time(item.finished_at ?? item.started_at, {
                        hour: 'numeric',
                        minute: '2-digit',
                        second: '2-digit',
                      })}
                    </td>
                    <td>{STEP_DEFS.find((step) => step.key === item.stage)?.name ?? item.stage}</td>
                    <td>{item.item}</td>
                    <td className="text-ink-200">{resultText(item.stage, item.status)}</td>
                    <td className="text-right text-caption text-ink-300">
                      {item.seconds == null ? '—' : `${item.seconds.toFixed(1)} s`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!job.recent_items.length && (
              <p className="type-caption text-ink-300">Waiting for the first item.</p>
            )}
          </div>
        </div>
        <Sources overview={overview} job={job} running />
      </div>
    </Panel>
  );
}
