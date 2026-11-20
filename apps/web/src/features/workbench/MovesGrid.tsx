import type React from 'react';
import { memo } from 'react';
import { Badge, Panel, Skeleton } from '@/components/ui';
import { Delta, StateNotice } from '@/components/catalyst';
import type { HistoryOutcome } from '@/features/history/outcome';
import { historyOutcome } from '@/features/history/outcome';
import type { components } from '@/api/schema';
import { cleanListingTags, formatDay } from '@/lib/format';

type MoveRow = { date: string; movePct: number };
type Run = components['schemas']['RunHistoryItem'];
export const MovesGrid = memo(function MovesGrid({
  rows,
  ticker,
  loading,
  explainedDays,
  runs,
  onSelectDate,
}: {
  rows: MoveRow[];
  ticker: string;
  loading: boolean;
  explainedDays: { date: string; tone: HistoryOutcome['tone']; label: string }[];
  runs: Run[];
  onSelectDate: (date: string) => void;
}): React.JSX.Element {
  const explained = new Map(explainedDays.map((item) => [item.date, item]));
  const runsByDate = new Map<string, Run>();
  runs.forEach((run) => {
    if (run.session_date && !runsByDate.has(run.session_date)) {
      runsByDate.set(run.session_date, run);
    }
  });
  return (
    <Panel
      title={
        <>
          Largest moves this year{' '}
          <span className="type-caption text-ink-300">
            The biggest daily moves for {ticker} · select a day to explore its news
          </span>
        </>
      }
      className="h-auto shrink-0"
    >
      {loading ? (
        <div className="moves-grid">
          {Array.from({ length: 6 }, (_, i) => (
            <Skeleton key={i} height={88} />
          ))}
        </div>
      ) : rows.length ? (
        <div className="moves-grid">
          {rows.map((row) => {
            const dayLabel = formatDay(row.date, true);
            const result = explained.get(row.date);
            const run = runsByDate.get(row.date);
            return (
              <button
                key={row.date}
                type="button"
                onClick={() => onSelectDate(row.date)}
                className="move-card flex flex-col items-start gap-2 rounded-md border border-line-100 bg-bg-200 p-3 text-left hover:border-line-200 hover:bg-bg-300"
              >
                <span className="flex w-full items-center justify-between gap-2">
                  <Delta value={row.movePct} className="type-title" />
                  <span className="type-body-sm text-ink-200">{dayLabel}</span>
                </span>
                {run?.cause && <span className="move-card-cause">{cleanListingTags(run.cause)}</span>}
                {!run && <span className="type-caption text-ink-300">Not explained yet · Explain</span>}
                {run ? (
                  <Badge tone={historyOutcome(run).tone}>{historyOutcome(run).label}</Badge>
                ) : result ? (
                  <Badge tone={result.tone}>{result.label}</Badge>
                ) : null}
              </button>
            );
          })}
        </div>
      ) : (
        <StateNotice
          title="No large moves in this range"
          body="Try a longer range (1Y) or click any candle to explain that day."
        />
      )}
    </Panel>
  );
});
