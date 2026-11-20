import type React from 'react';
import { useState } from 'react';
import { X } from 'lucide-react';
import { Button, Skeleton } from '@/components/ui';
import { PriceChart, StateNotice, type PriceRow } from '@/components/catalyst';
import type { components } from '@/api/schema';
import { formatCompanyName, formatDay } from '@/lib/format';
import type { HistoryOutcome } from '@/features/history/outcome';

type Ticker = components['schemas']['SelectableTicker'];
type OhlcvQuery = { isLoading: boolean; isError: boolean; refetch: () => Promise<unknown> };
export type ChartPanelProps = {
  ticker: string;
  coveredCount: number;
  tickerCovered: boolean;
  coverageLoaded: boolean;
  onBrowseTickers: () => void;
  tickerRow: Ticker | undefined;
  ohlcv: OhlcvQuery;
  candles: PriceRow[];
  visibleFrom: string;
  date: string;
  onClearDate: () => void;
  explainedDates: { date: string; tone: HistoryOutcome['tone']; label: string }[];
  currentBar: PriceRow | undefined;
  onSelectDate: (date: string) => void;
  selectableDates: readonly string[];
  onHover: (row: PriceRow | null) => void;
  coverageStart: string | null;
};

function changeFor(bar: PriceRow | undefined, candles: PriceRow[]): number | null {
  if (!bar) return null;
  const index = candles.findIndex((item) => item.date === bar.date);
  const previous = index > 0 ? candles[index - 1] : undefined;
  return previous ? (bar.close / previous.close - 1) * 100 : null;
}

function signed(value: number): string {
  return `${value < 0 ? '−' : '+'}${Math.abs(value).toFixed(2)}`;
}

export function ChartPanel({
  ticker,
  coveredCount,
  tickerCovered,
  coverageLoaded,
  onBrowseTickers,
  tickerRow,
  ohlcv,
  candles,
  visibleFrom,
  date,
  onClearDate,
  explainedDates,
  currentBar,
  onSelectDate,
  selectableDates,
  onHover,
  coverageStart,
}: ChartPanelProps): React.JSX.Element {
  const visibleSelectableCount = selectableDates.filter(
    (value) => value >= visibleFrom && (!candles.at(-1) || value <= candles.at(-1)!.date),
  ).length;
  const explainable = Boolean(date && selectableDates.includes(date));
  const latest = candles.at(-1);
  const latestChange = changeFor(latest, candles);
  const currentChange = changeFor(currentBar, candles);
  const signVolume = (value: number) => `${(value / 1_000_000).toFixed(1)}M`;

  return (
    <section className="chart-panel h-full overflow-hidden rounded-lg border border-line-100 bg-bg-100">
      {!coverageLoaded ? (
        <div className="h-full p-4">
          <Skeleton className="h-full" />
        </div>
      ) : !tickerCovered ? (
        <div className="p-4">
          <StateNotice
            tone="warn"
            title="No prices for this ticker"
            body={`${ticker} is not in the ${coveredCount} covered tickers, so there is nothing to explain yet.`}
            action={{ label: 'Browse covered tickers', onClick: onBrowseTickers }}
          />
        </div>
      ) : ohlcv.isLoading ? (
        <div className="h-full p-4">
          <Skeleton className="h-full" />
        </div>
      ) : ohlcv.isError ? (
        <div className="flex items-center gap-2 p-4 type-body-sm text-ink-300">
          Could not load chart data.
          <Button size="sm" onClick={() => void ohlcv.refetch()}>
            Retry
          </Button>
        </div>
      ) : candles.length === 0 ? (
        <div className="p-4">
          <StateNotice
            tone="warn"
            title={`No prices for ${ticker} yet`}
            body={`This dataset has no daily prices for ${ticker} in the chart range, so there is nothing to explain.`}
            action={{ label: 'Browse covered tickers', onClick: onBrowseTickers }}
          />
        </div>
      ) : (
        <>
          <div className="flex flex-wrap items-end justify-between gap-4 px-5 pb-3 pt-4">
            <div className="flex min-w-0 flex-col gap-1">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span className="type-title font-numeric font-semibold text-ink-100">{ticker}</span>
                <span className="type-body text-ink-200">{formatCompanyName(tickerRow?.name ?? '')}</span>
                {tickerRow?.sector_etf && (
                  <span className="type-caption text-ink-300">peers vs {tickerRow.sector_etf}</span>
                )}
              </div>
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                {latest && (
                  <span className="type-display font-numeric text-ink-100">{latest.close.toFixed(2)}</span>
                )}
                {latestChange != null && (
                  <span
                    className={`font-numeric type-body-sm ${latestChange >= 0 ? 'text-up' : 'text-down'}`}
                  >
                    {signed(latest!.close - (candles.at(-2)?.close ?? latest!.close))} ({signed(latestChange)}
                    %)
                  </span>
                )}
                {latest && (
                  <span className="type-caption text-ink-300">
                    Close · {formatDay(latest.date)} · latest data
                  </span>
                )}
              </div>
            </div>
          </div>
          <div className="flex min-h-6 flex-wrap items-center justify-between gap-2 px-5 pb-2">
            <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 font-numeric type-body-sm">
              <span className="text-ink-100">{currentBar ? formatDay(currentBar.date) : '—'}</span>
              {(['open', 'high', 'low', 'close'] as const).map((key) => (
                <span key={key} className="text-ink-300">
                  {key[0]!.toUpperCase()}{' '}
                  <span
                    className={
                      currentChange == null ? 'text-ink-200' : currentChange >= 0 ? 'text-up' : 'text-down'
                    }
                  >
                    {currentBar?.[key].toFixed(2) ?? '—'}
                  </span>
                </span>
              ))}
              {currentChange != null && (
                <span className={currentChange >= 0 ? 'text-up' : 'text-down'}>{signed(currentChange)}%</span>
              )}
              <span className="text-ink-300">
                Vol <span className="text-ink-200">{currentBar ? signVolume(currentBar.volume) : '—'}</span>
              </span>
            </div>
            {date && (
              <span
                className={`inline-flex items-center gap-1 rounded-full pl-3 pr-1 type-caption font-medium ${explainable ? 'bg-accent-soft text-accent' : 'bg-bg-300 text-ink-100'}`}
              >
                {explainable ? 'Explaining' : 'Viewing'} {formatDay(date)}
                <button
                  type="button"
                  aria-label="Clear the selected day"
                  className="clear-selected-day"
                  onClick={onClearDate}
                >
                  <X size={12} strokeWidth={1.7} />
                </button>
              </span>
            )}
          </div>
          <div className="chart-canvas relative min-h-0 px-1">
            <PriceChart
              rows={candles}
              explainedDates={explainedDates}
              selectedDate={date}
              onSelectDate={onSelectDate}
              selectableDates={selectableDates}
              onHover={onHover}
              coverageStart={coverageStart}
            />
          </div>
          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line-100 px-5 py-3 type-caption text-ink-300">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
              <span className="inline-flex items-center gap-2">
                <i className="legend-lane-mark chart-lane-mark-ready" />
                Can be explained · moved 2% or more ({visibleSelectableCount} days)
              </span>
              {explainedDates.length > 0 && (
                <span className="inline-flex items-center gap-2">
                  <i className="legend-lane-mark chart-lane-mark-explained" />
                  Already explained
                </span>
              )}
            </div>
            <span>Click any day to see its news · drag or swipe sideways to move through time</span>
          </div>
        </>
      )}
    </section>
  );
}
