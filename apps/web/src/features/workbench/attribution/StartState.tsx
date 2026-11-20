import type React from 'react';
import { Badge, Button } from '@/components/ui';
import { Delta } from '@/components/catalyst';
import type { components } from '@/api/schema';
import { cleanListingTags, formatDay, formatPct } from '@/lib/format';
import { historyOutcome } from '@/features/history/outcome';
import { FirstVisit, type DaySuggestion } from './FirstVisit';

type Run = components['schemas']['RunHistoryItem'];
export type StartStateProps = {
  ticker?: string;
  tickerRuns?: Run[];
  recentRuns?: Run[];
  onOpenRun?: (run: Run) => void;
  suggestions?: DaySuggestion[];
  onPickDay?: (date: string) => void;
};

function finishedWithCause(run: Run): boolean {
  return (
    run.lifecycle_status === 'COMPLETED' &&
    Boolean(run.ticker) &&
    Boolean(run.cause?.trim()) &&
    Boolean(run.session_date)
  );
}

export function StartState({
  ticker = '',
  tickerRuns = [],
  recentRuns = [],
  onOpenRun = () => undefined,
  suggestions = [],
  onPickDay = () => undefined,
}: StartStateProps): React.JSX.Element {
  // The newest trading day with an answer, not the newest run: a re-run of an old day is not news.
  const latest = tickerRuns
    .filter(finishedWithCause)
    .sort((a, b) => (b.session_date ?? '').localeCompare(a.session_date ?? ''))
    .at(0);
  // One row per ticker, so the list shows what Catalyst covers rather than one stock's history.
  const seenTickers = new Set<string>();
  const recent = recentRuns
    .filter(finishedWithCause)
    .filter((run) => {
      const key = run.ticker ?? '';
      if (seenTickers.has(key)) return false;
      seenTickers.add(key);
      return true;
    })
    .slice(0, 4);
  // Nothing explained yet in this browser: show the first-visit guide instead of empty lists.
  if (!latest && recent.length === 0) {
    return <FirstVisit ticker={ticker} suggestions={suggestions} onPickDay={onPickDay} />;
  }
  return (
    <div className="start-state-content">
      {latest && (
        <section className="start-answer-card">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="type-heading m-0">Latest answer for {ticker}</h2>
            <Badge tone={historyOutcome(latest).tone}>{historyOutcome(latest).label}</Badge>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            {typeof latest.move_pct === 'number' && (
              <Delta value={latest.move_pct} className="type-title font-numeric" />
            )}
            <span className="type-body-sm text-ink-300">{formatDay(latest.session_date ?? '')}</span>
          </div>
          <p className="start-answer-cause">{cleanListingTags(latest.cause ?? '')}</p>
          <Button variant="primary" onClick={() => onOpenRun(latest)}>
            Open this answer
          </Button>
        </section>
      )}
      {recent.length > 0 && (
        <section className="flex flex-col gap-2">
          <h2 className="type-label m-0 text-ink-300">Recently explained</h2>
          <div className="recent-answer-list">
            {recent.map((run) => (
              <button
                key={run.run_id}
                type="button"
                className="recent-answer-row"
                onClick={() => onOpenRun(run)}
              >
                <span className="font-numeric type-body-sm text-ink-100">{run.ticker}</span>
                <span className="recent-answer-summary">
                  <span className="recent-answer-cause">{cleanListingTags(run.cause ?? '')}</span>
                  <span className="type-caption text-ink-300">{formatDay(run.session_date ?? '')}</span>
                </span>
                {typeof run.move_pct === 'number' ? (
                  <span
                    className={`font-numeric type-body-sm ${run.move_pct > 0 ? 'text-up' : run.move_pct < 0 ? 'text-down' : 'text-ink-200'}`}
                  >
                    {formatPct(run.move_pct)}
                  </span>
                ) : (
                  <span />
                )}
              </button>
            ))}
          </div>
        </section>
      )}
      <p className="m-0 type-body-sm text-ink-300">
        Pick a day on the chart to see the news around the move and open an explanation.
      </p>
    </div>
  );
}
