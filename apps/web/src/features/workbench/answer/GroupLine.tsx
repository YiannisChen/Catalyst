import type React from 'react';
import type { AnswerModel } from './model';
import { formatPct } from '@/lib/format';

function formatPoints(value: number): string {
  const sign = value < 0 ? '−' : value > 0 ? '+' : '';
  return `${sign}${Math.abs(value).toFixed(2)}`;
}

export function GroupLine({ answer }: { answer: AnswerModel }): React.JSX.Element | null {
  if (!answer.relative.length) return null;
  const max = Math.max(3, ...answer.relative.map((row) => Math.abs(row.value)));
  const residual = answer.excessVsSector ?? answer.group?.residualPct ?? null;
  const rows = [...answer.relative].sort((a, b) => {
    const order = { target: 0, peers: 1, sector: 2, market: 3 };
    return order[a.kind] - order[b.kind];
  });
  return (
    <section className="polish-answer-group">
      <div className="polish-answer-group-heading">
        <h2 className="type-label text-ink-300">Against its group</h2>
        {residual != null && (
          <span
            className={`font-numeric polish-answer-residual ${residual > 0 ? 'text-up' : residual < 0 ? 'text-down' : 'text-ink-200'}`}
          >
            {formatPoints(residual)} vs sector
          </span>
        )}
      </div>
      <div className="polish-answer-group-rows">
        {rows.map((row) => {
          const width = Math.min(50, Math.max(1, (Math.abs(row.value) / max) * 50));
          return (
            <div key={row.kind} className="polish-answer-group-row">
              <span className="type-body-sm text-ink-200">{row.label}</span>
              <span className="polish-answer-bar" aria-hidden="true">
                <span
                  className={`polish-answer-bar-fill ${row.value >= 0 ? 'polish-answer-bar-positive' : 'polish-answer-bar-negative'}`}
                  style={{ left: row.value < 0 ? `${50 - width}%` : '50%', width: `${width}%` }}
                />
              </span>
              <span
                className={`font-numeric polish-answer-group-value ${row.value > 0 ? 'text-up' : row.value < 0 ? 'text-down' : 'text-ink-200'}`}
              >
                {formatPct(row.value)}
              </span>
            </div>
          );
        })}
      </div>
    </section>
  );
}
