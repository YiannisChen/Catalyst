import type { JSX } from 'react';
import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { useRunHistory } from '@/api/queries';
import type { components } from '@/api/schema';
import { Button } from '@/components/ui';
import { HistoryFilters, type HistoryFilter } from '@/features/history/HistoryFilters';
import { HistoryPaging, HistoryTable } from '@/features/history/HistoryTable';
import { HistorySummary } from '@/features/history/HistorySummary';

type RunHistoryItem = components['schemas']['RunHistoryItem'];
const pageSize = 25;

export default function History(): JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const tickerParam = (searchParams.get('ticker') ?? '').toUpperCase();
  const outcomeParam = searchParams.get('outcome') ?? '';
  const outcome: HistoryFilter = [
    'ANSWERED',
    'REPORTED',
    'NO_CAUSE',
    'ABSTAINED',
    'TIER_ONLY',
    'FAILED',
  ].includes(outcomeParam)
    ? (outcomeParam as HistoryFilter)
    : '';
  const [tickerDraft, setTickerDraft] = useState(tickerParam);
  const [cursorStack, setCursorStack] = useState<Array<string | undefined>>([]);
  const [cursor, setCursor] = useState<string | undefined>();
  const history = useRunHistory({
    limit: pageSize,
    ...(tickerParam ? { ticker: tickerParam } : {}),
    ...(outcome ? { outcome } : {}),
    ...(cursor ? { cursor } : {}),
  });

  useEffect(() => setTickerDraft(tickerParam), [tickerParam]);
  useEffect(() => {
    setCursor(undefined);
    setCursorStack([]);
  }, [outcome, tickerParam]);
  useEffect(() => {
    if (tickerDraft === tickerParam) return;
    const timeout = window.setTimeout(() => applyFilters(tickerDraft, outcome), 300);
    return () => window.clearTimeout(timeout);
  }, [outcome, tickerDraft, tickerParam]);

  function applyFilters(ticker: string, nextOutcome: HistoryFilter) {
    const next = new URLSearchParams(searchParams);
    const normalizedTicker = ticker.trim().toUpperCase();
    if (normalizedTicker) next.set('ticker', normalizedTicker);
    else next.delete('ticker');
    if (nextOutcome) next.set('outcome', nextOutcome);
    else next.delete('outcome');
    setSearchParams(next);
    setCursor(undefined);
    setCursorStack([]);
  }

  function openRun(item: RunHistoryItem) {
    const next = new URLSearchParams();
    if (item.ticker) next.set('ticker', item.ticker);
    if (item.session_date) next.set('date', item.session_date);
    next.set('run', item.run_id);
    navigate(`/?${next.toString()}`);
  }

  const items = history.data?.items ?? [];
  const hasFilters = Boolean(tickerParam || outcome);
  const clearFilters = () => applyFilters('', '');

  return (
    <main className="mx-auto flex h-full w-full max-w-7xl flex-col gap-4 overflow-auto px-5 py-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="type-display m-0">History</h1>
          <p className="type-body m-0 text-ink-200">
            Every run you started, newest first. Open a row to see its answer in the workbench.
          </p>
        </div>
        {history.data?.summary && <HistorySummary summary={history.data.summary} />}
      </div>

      <section className="flex min-w-0 flex-col overflow-hidden rounded-lg border border-line-100 bg-bg-100">
        <HistoryFilters
          ticker={tickerDraft}
          outcome={outcome}
          onTickerChange={setTickerDraft}
          onTickerSubmit={() => applyFilters(tickerDraft, outcome)}
          onOutcomeChange={(next) => applyFilters(tickerParam, next)}
        />
        {history.isError ? (
          <div className="flex items-center justify-between px-4 py-5 text-body-sm text-ink-300">
            <span>Run history is unavailable right now.</span>
            <Button variant="ghost" size="sm" onClick={() => void history.refetch()}>
              Retry
            </Button>
          </div>
        ) : history.isLoading ? (
          <HistoryTable items={[]} loading onOpen={openRun} />
        ) : items.length === 0 ? (
          <div className="flex flex-col items-start gap-3 px-5 py-7">
            <p className="type-body m-0 text-ink-200">
              {hasFilters
                ? 'No runs match these filters.'
                : 'No runs yet. Pick a session in the workbench and press Explain.'}
            </p>
            {hasFilters ? (
              <Button variant="ghost" size="sm" onClick={clearFilters}>
                Clear filters
              </Button>
            ) : (
              <Button variant="ghost" size="sm" onClick={() => navigate('/')}>
                Open workbench
              </Button>
            )}
          </div>
        ) : (
          <>
            <HistoryTable items={items} onOpen={openRun} />
            <HistoryPaging
              count={items.length}
              offset={cursorStack.length * pageSize}
              total={history.data?.summary?.matching ?? null}
              canPrevious={cursorStack.length > 0}
              canNext={Boolean(history.data?.next_cursor)}
              onPrevious={() => {
                setCursorStack((stack) => {
                  const next = stack.slice(0, -1);
                  setCursor(next.length ? next[next.length - 1] : undefined);
                  return next;
                });
              }}
              onNext={() => {
                const nextCursor = history.data?.next_cursor;
                if (!nextCursor) return;
                setCursorStack((stack) => [...stack, cursor]);
                setCursor(nextCursor);
              }}
            />
          </>
        )}
      </section>
    </main>
  );
}
