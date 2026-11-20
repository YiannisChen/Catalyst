import type { JSX } from 'react';
import type { components } from '@/api/schema';
import { fmtDate } from '@/features/data/model';
import { time } from './dataPage';
export function RunChart({ items }: { items: components['schemas']['DataJobHistoryItem'][] }): JSX.Element {
  const runs = [...items].slice(0, 14).reverse();
  const max = Math.max(1, ...runs.map((r) => r.duration_seconds ?? 0));
  const color = (status: string) =>
    status === 'COMPLETED' ? 'var(--up)' : status === 'PARTIAL' ? 'var(--warn)' : 'var(--ink-300)';
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-baseline justify-between">
        <span className="type-label text-ink-300">Last 14 runs · minutes</span>
        <span className="type-caption text-ink-300">
          <i className="data-legend-up" /> Complete <i className="data-legend-warn" /> Partial{' '}
          <i className="data-legend-neutral" /> Interrupted
        </span>
      </div>
      <div className="polish-data-run-chart">
        {runs.map((run) => {
          const minutes = run.duration_seconds == null ? 0 : Math.round(run.duration_seconds / 60);
          return (
            <div
              key={run.job_id}
              className="data-run-column"
              title={`${fmtDate(run.finished_at ?? run.created_at)} · ${minutes} min · ${run.status}`}
            >
              <span className="font-numeric text-ink-300">{minutes || '—'}</span>
              <span
                className="data-run-bar"
                style={{
                  height: `${Math.max(4, ((run.duration_seconds ?? 0) / max) * 65)}px`,
                  background: color(run.status),
                }}
              />
            </div>
          );
        })}
      </div>
      <div className="polish-data-run-dates" aria-hidden="true">
        {runs.map((run, index) => (
          <span key={run.job_id} className="font-numeric text-ink-300">
            {index % 2 === 0
              ? time(run.finished_at ?? run.created_at, { month: '2-digit', day: '2-digit' })
              : ''}
          </span>
        ))}
      </div>
    </div>
  );
}
