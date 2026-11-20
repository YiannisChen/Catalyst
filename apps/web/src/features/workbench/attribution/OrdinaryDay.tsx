import type React from 'react';
import { formatDay, formatPct } from '@/lib/format';

export type OrdinaryDayProps = {
  ticker: string;
  date: string;
  move: number;
  tradingDay?: boolean;
  newsCount: number | null;
  coverageStart: string | null; // the earliest day Catalyst can explain, for any ticker
};

export type UnexplainableKind = 'ordinary' | 'before_coverage' | 'not_prepared';

/** Why a picked day has no Explain button. The 2% rule is close to close. */
export function unexplainableKind(
  date: string,
  move: number,
  coverageStart: string | null,
): UnexplainableKind {
  if (Math.abs(move) < 2) return 'ordinary';
  if (coverageStart && date < coverageStart) return 'before_coverage';
  return 'not_prepared';
}

export const unexplainableBadge: Record<UnexplainableKind, string> = {
  ordinary: 'Ordinary move',
  before_coverage: 'Before coverage',
  not_prepared: 'Not prepared yet',
};

/** A day without an Explain button: an ordinary move, a day before coverage, or a day not prepared yet. */
export function OrdinaryDay({
  ticker,
  date,
  move,
  tradingDay = true,
  newsCount,
  coverageStart,
}: OrdinaryDayProps): React.JSX.Element {
  const kind = unexplainableKind(date, move, coverageStart);
  if (!tradingDay) {
    return (
      <div className="flex flex-col gap-3">
        <span className="type-caption text-ink-300">
          {formatDay(date, true)} · {ticker}
        </span>
        <p className="m-0 type-body text-ink-200">
          No trading session on this date. Pick a day on the chart.
        </p>
      </div>
    );
  }
  const headlines =
    newsCount == null
      ? ' The day’s headlines are loading on the left.'
      : newsCount > 0
        ? ` The day’s ${newsCount} headlines are on the left.`
        : ' There are no headlines for this day.';
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <span className="type-caption text-ink-300">
          {formatDay(date, true)} · {ticker}
        </span>
        <span className="type-title">
          {ticker} {move < 0 ? 'fell' : 'rose'}{' '}
          <span className={`font-numeric ${move < 0 ? 'text-down' : 'text-up'}`}>{formatPct(move)}</span>
        </span>
      </div>
      {kind === 'ordinary' ? (
        <p className="m-0 type-body text-ink-200">
          This is an ordinary move, under 2% from the previous close, so Catalyst does not look for a cause.
          {headlines}
        </p>
      ) : kind === 'before_coverage' ? (
        <p className="m-0 type-body text-ink-200">
          This day is before Catalyst&apos;s coverage, so it cannot be explained.
          {coverageStart ? ` Explanations start on ${formatDay(coverageStart, true)}.` : ''}
          {newsCount === 0 ? ' The news archive does not reach this day either.' : headlines}
        </p>
      ) : (
        <p className="m-0 type-body text-ink-200">
          This day moved 2% or more, but its news and filings are not prepared yet, so it cannot be explained
          now. A later data update can add it.
          {headlines}
        </p>
      )}
      <p className="m-0 type-caption text-ink-300">
        Days with a mark under the chart moved 2% or more and can be explained.
      </p>
    </div>
  );
}
