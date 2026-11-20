import { useCallback, useMemo } from 'react';
import { useSearchParams } from 'react-router';
import type { components } from '@/api/schema';
import {
  useModelsCatalog,
  useOhlcv,
  useOptions,
  useRangeLocal,
  useRecentRuns,
  useRunsForSession,
  useRunsForTicker,
  useSession,
  useSessionNews,
} from '@/api/queries';
import type { PriceRow } from '@/components/catalyst';
import { largeMoves } from '@/lib/moves';
import { historyOutcome, type HistoryOutcome } from '@/features/history/outcome';
// Stable when a ticker has no ready sessions, so the chart does not recolor on every render.
const noSessions: string[] = [];

function dateMonthsBefore(date: string, months: number): string {
  const value = new Date(`${date}T00:00:00Z`);
  const day = value.getUTCDate();
  value.setUTCDate(1);
  value.setUTCMonth(value.getUTCMonth() - months);
  value.setUTCMonth(value.getUTCMonth() + 1, 0);
  value.setUTCDate(Math.min(day, value.getUTCDate()));
  return value.toISOString().slice(0, 10);
}

export type WorkbenchState = {
  options: ReturnType<typeof useOptions>;
  tickerOptions: components['schemas']['SelectableTicker'][];
  tickerRow: components['schemas']['SelectableTicker'] | undefined;
  ticker: string;
  requestedTicker: string | null;
  tickerCovered: boolean;
  sessions: string[];
  coverageStart: string | null; // earliest explainable day across all tickers
  date: string;
  range: string;
  /** The selected day can be explained (a listed session, or a saved run or case). */
  explainable: boolean;
  nonTradingDate: boolean;
  run: string | null;
  caseId: string | null;
  tab: 'answer' | 'process' | 'run';
  params: URLSearchParams;
  update: (values: Record<string, string | null>) => void;
  selectDate: (value: string) => void;
  ohlcv: ReturnType<typeof useOhlcv>;
  session: ReturnType<typeof useSession>;
  news: ReturnType<typeof useSessionNews>;
  sessionRuns: ReturnType<typeof useRunsForSession>;
  explainedDays: { date: string; tone: HistoryOutcome['tone']; label: string }[];
  recentRuns: ReturnType<typeof useRecentRuns>;
  catalog: ReturnType<typeof useModelsCatalog>;
  candles: PriceRow[];
  moveRows: ReturnType<typeof largeMoves>; // in the visible range
  allMoves: ReturnType<typeof largeMoves>; // every loaded candle, for chart markers
  start: string;
  visibleFrom: string;
  windowEnd: string;
};

export function useWorkbenchState(): WorkbenchState {
  const options = useOptions();
  const [params, setParams] = useSearchParams();
  const tickerOptions = options.data?.tickers ?? [];
  const requestedTicker = params.get('ticker');
  // An uncovered ticker in the URL is kept (and shown as such), not swapped for the first covered one.
  // Until options load, nothing is judged uncovered, so the notice does not flash.
  const validTicker =
    !options.data || !requestedTicker || tickerOptions.some((item) => item.ticker === requestedTicker);
  const tickerRow = validTicker
    ? (tickerOptions.find((item) => item.ticker === requestedTicker) ?? tickerOptions[0])
    : undefined;
  const ticker = tickerRow?.ticker ?? '';
  const sessions = options.data?.sessions[ticker] ?? noSessions;
  const coverageStart = useMemo(() => {
    const firsts = Object.values(options.data?.sessions ?? {}).flatMap((days) =>
      days.length ? [days[0]!] : [],
    );
    return firsts.length ? firsts.sort()[0]! : null;
  }, [options.data]);
  const requestedDate = params.get('date');
  // Any trading day can be opened to read its news; only the listed sessions (moves of 2% or more)
  // can be explained, plus a saved run or featured case on any day.
  const openingSaved = Boolean(params.get('run') || params.get('case'));
  const date = validTicker && requestedDate && /^\d{4}-\d{2}-\d{2}$/.test(requestedDate) ? requestedDate : '';
  const explainable = Boolean(date && (sessions.includes(date) || openingSaved));
  // The chart always shows one year.
  const range = '1Y';
  const run = params.get('run');
  const caseId = params.get('case');
  const tabParam = params.get('tab');
  const tab: WorkbenchState['tab'] = tabParam === 'process' || tabParam === 'run' ? tabParam : 'answer';
  const rangeLocal = useRangeLocal();
  // The chart runs to the newest daily bar, which can be later than the newest explainable session.
  const lastSession =
    [sessions.at(-1) ?? options.data?.last_trading_day ?? '', rangeLocal.data?.max_date ?? '']
      .sort()
      .at(-1) ?? '';
  const months = 12;
  // The chart always runs to the newest bar; an older selected day is scrolled into view by the chart,
  // so the header's "latest data" stays the real latest close.
  const windowEnd = lastSession;
  const defaultStart = windowEnd ? dateMonthsBefore(windowEnd, 36) : '';
  const start = date && defaultStart && date < defaultStart ? dateMonthsBefore(date, 6) : defaultStart;
  const visibleFrom = windowEnd ? dateMonthsBefore(windowEnd, months) : '';
  const ohlcv = useOhlcv(ticker, start, windowEnd);
  const session = useSession(ticker, date);
  const nonTradingDate = session.data?.is_trading_day === false;
  const news = useSessionNews(ticker, date, params.get('run'));
  const sessionRuns = useRunsForSession(ticker, date);
  const tickerRuns = useRunsForTicker(ticker);
  const explainedDays = useMemo(() => {
    const seen = new Set<string>();
    return (tickerRuns.data?.items ?? []).flatMap((item) => {
      if (!item.session_date || seen.has(item.session_date)) return [];
      seen.add(item.session_date);
      const outcome = historyOutcome(item);
      return outcome.label === 'Running'
        ? []
        : [{ date: item.session_date, tone: outcome.tone, label: outcome.label }];
    });
  }, [tickerRuns.data]);
  const recentRuns = useRecentRuns(5);
  const catalog = useModelsCatalog();
  const update = useCallback(
    (values: Record<string, string | null>) => {
      const next = new URLSearchParams(params);
      Object.entries(values).forEach(([key, value]) => (value ? next.set(key, value) : next.delete(key)));
      if ('ticker' in values || 'date' in values || 'run' in values) next.delete('case');
      setParams(next);
    },
    [params, setParams],
  );
  const selectDate = useCallback(
    (value: string) => {
      const completed = (tickerRuns.data?.items ?? []).find(
        (item) => item.session_date === value && item.lifecycle_status === 'COMPLETED',
      );
      update({ ticker, date: value, run: completed?.run_id ?? null, case: null });
    },
    [update, ticker, tickerRuns.data],
  );
  const candles = useMemo(
    () =>
      (ohlcv.data?.candles ?? []).flatMap((item) =>
        item.open != null && item.high != null && item.low != null && item.close != null
          ? [
              {
                date: item.date,
                open: item.open,
                high: item.high,
                low: item.low,
                close: item.close,
                volume: item.volume ?? 0,
              },
            ]
          : [],
      ),
    [ohlcv.data],
  );
  const allMoves = useMemo(() => largeMoves(ohlcv.data?.candles ?? []), [ohlcv.data]);
  const moveRows = useMemo(
    () => allMoves.filter((item) => item.date >= visibleFrom),
    [allMoves, visibleFrom],
  );
  return {
    options,
    tickerOptions,
    tickerRow,
    ticker,
    requestedTicker,
    tickerCovered: validTicker,
    sessions,
    coverageStart,
    date,
    range,
    explainable,
    nonTradingDate,
    run,
    caseId,
    tab,
    params,
    update,
    selectDate,
    ohlcv,
    session,
    news,
    sessionRuns,
    explainedDays,
    recentRuns,
    catalog,
    candles,
    moveRows,
    allMoves,
    start,
    visibleFrom,
    windowEnd,
  };
}
