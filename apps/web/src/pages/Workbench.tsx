import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { usePageControls } from '@/app/PageControls';
import { useWorkbenchKeys } from '@/hooks/useWorkbenchKeys';
import { useAnalysisModel } from '@/lib/useAnalysisModel';
import { useQuota, useRangeLocal, useRecentRuns, useRunsForTicker } from '@/api/queries';
import type { AnswerQuote } from '@/features/workbench/answer/model';
import { RunContent } from '@/features/workbench/run/RunContent';
import { ReplayContent } from '@/features/workbench/run/ReplayContent';
import { useExplainShortcut, useRunStarter } from '@/features/workbench/run/useRunStarter';
import { summarizeSession } from '@/features/workbench/sessionSummary';
import { decodeEntities } from '@/lib/format';
import { TickerPalette } from '@/features/workbench/TickerPalette';
import { TopBarControls } from '@/features/workbench/TopBarControls';
import { useWorkbenchState } from '@/features/workbench/useWorkbenchState';
import { ChartPanel } from '@/features/workbench/ChartPanel';
import { NewsPanel } from '@/features/workbench/NewsPanel';
import { MovesGrid } from '@/features/workbench/MovesGrid';
import { WorkbenchLayout } from '@/features/workbench/WorkbenchLayout';
import { AttributionPanel } from '@/features/workbench/attribution/AttributionPanel';
import { RunDialog } from '@/features/workbench/run/RunDialog';
import type { components } from '@/api/schema';
import type { PriceRow } from '@/components/catalyst';
const noNewsItems: never[] = [];

export default function Workbench(): React.JSX.Element {
  const state = useWorkbenchState();
  const { setControls } = usePageControls();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [hover, setHover] = useState<PriceRow | null>(null);
  const [runHeaderActions, setRunHeaderActions] = useState<React.ReactNode>(null);
  const [selectedNewsId, setSelectedNewsId] = useState<string | null>(null);
  const [selectedQuoteId, setSelectedQuoteId] = useState<string | null>(null);
  const [openOnOwnKey, setOpenOnOwnKey] = useState(false);
  const quota = useQuota();
  const analysis = useAnalysisModel();
  const rangeLocal = useRangeLocal();
  const tickerRuns = useRunsForTicker(state.ticker);
  const recentRuns = useRecentRuns(50);
  const clearRunView = useCallback(() => {
    setRunHeaderActions(null);
    setSelectedQuoteId(null);
    setSelectedNewsId(null);
  }, []);
  const openRun = useCallback((runId: string) => state.update({ run: runId, tab: null }), [state.update]);
  const runs = useRunStarter({
    ticker: state.ticker,
    date: state.date,
    catalog: state.catalog.data,
    quota: quota.data,
    onBeforeStart: clearRunView,
    onStarted: openRun,
  });
  useEffect(() => {
    const runAgain = () => runs.retry();
    window.addEventListener('catalyst:run-again', runAgain);
    return () => window.removeEventListener('catalyst:run-again', runAgain);
  }, [runs.retry]);
  const hasSavedBrowserKey = Object.values(analysis.keys).some((value) => Boolean(value?.key));
  const selectTicker = (value: string) => {
    state.update({ ticker: value, date: null, run: null, case: null });
    setPaletteOpen(false);
  };
  const selectRun = (item: components['schemas']['RunHistoryItem']) => {
    if (item.ticker && item.session_date) {
      setSelectedQuoteId(null);
      setSelectedNewsId(null);
      state.update({ ticker: item.ticker, date: item.session_date, run: item.run_id, case: null, tab: null });
      setPaletteOpen(false);
    }
  };
  const step = useCallback(
    (direction: -1 | 1) => {
      const index = state.sessions.indexOf(state.date);
      const next = Math.max(
        0,
        Math.min(state.sessions.length - 1, (index < 0 ? state.sessions.length - 1 : index) + direction),
      );
      if (state.sessions[next]) state.selectDate(state.sessions[next]!);
    },
    [state.sessions, state.date, state.selectDate],
  );
  useWorkbenchKeys(
    () => setPaletteOpen(true),
    () => {
      if (state.date) state.update({ date: null, run: null, case: null });
    },
    step,
    paletteOpen || runs.dialogOpen,
  );
  const readyToRun =
    !paletteOpen &&
    !runs.dialogOpen &&
    Boolean(state.date) &&
    state.explainable &&
    !state.run &&
    !state.session.isLoading &&
    !state.news.isLoading &&
    !state.sessionRuns.isLoading &&
    !state.sessionRuns.data?.items.length;
  useExplainShortcut(readyToRun, runs.request);
  const resolvedModel = analysis.choice;
  const runDefaults = analysis.defaults;
  const modelLine = resolvedModel ? `${resolvedModel.providerLabel} · ${resolvedModel.modelId}` : '—';
  useEffect(() => {
    setControls(<TopBarControls onOpenPalette={() => setPaletteOpen(true)} />);
    return () => setControls(null);
  }, [setControls, runs.setDialogOpen]);
  const currentBar = hover ?? state.candles.find((item) => item.date === state.date) ?? state.candles.at(-1);
  const newestRun = state.sessionRuns.data?.items[0];
  // A stable empty list: a fresh [] each render would rebuild newsTitles and loop through the run view.
  const newsItems = state.news.data?.items ?? noNewsItems;
  const overnightCount = newsItems.filter((item) => item.phase === 'PRE_OPEN').length;

  // The ticker's three biggest explainable moves, for the first-visit guide.
  const daySuggestions = useMemo(() => {
    const explainable = new Set(state.sessions);
    return state.candles
      .flatMap((bar, index) => {
        const previous = state.candles[index - 1];
        if (!previous || !explainable.has(bar.date)) return [];
        return [{ date: bar.date, movePct: (bar.close / previous.close - 1) * 100 }];
      })
      .sort((a, b) => Math.abs(b.movePct) - Math.abs(a.movePct))
      .slice(0, 3);
  }, [state.candles, state.sessions]);
  const cutoffDate = state.news.data?.window.cutoff_at;
  // Cost and time estimates come from recent runs of the model analyses use now, not of every model.
  const modelKey = resolvedModel ? `${resolvedModel.provider}/${resolvedModel.modelId}` : '';
  const sameModelRuns = (recentRuns.data?.items ?? []).filter((item) => item.model === modelKey).slice(0, 5);
  const summary = summarizeSession(state.session.data, sameModelRuns);
  const chart = (
    <ChartPanel
      ticker={state.requestedTicker ?? state.ticker}
      coveredCount={state.tickerOptions.length}
      tickerCovered={state.tickerCovered}
      coverageLoaded={Boolean(state.options.data)}
      onBrowseTickers={() => setPaletteOpen(true)}
      tickerRow={state.tickerCovered ? state.tickerRow : undefined}
      ohlcv={state.ohlcv}
      candles={state.candles}
      visibleFrom={state.visibleFrom}
      date={state.date}
      onClearDate={() => state.update({ date: null, run: null })}
      explainedDates={state.explainedDays}
      currentBar={currentBar}
      onSelectDate={state.selectDate}
      coverageStart={state.coverageStart}
      selectableDates={state.sessions}
      onHover={setHover}
    />
  );
  const news = !state.date ? (
    <MovesGrid
      rows={state.moveRows}
      ticker={state.ticker}
      // Before the ticker list loads the price query is idle, not loading; show skeletons, not "no moves".
      loading={!state.ohlcv.data && !state.ohlcv.isError}
      explainedDays={state.explainedDays}
      runs={tickerRuns.data?.items ?? []}
      onSelectDate={state.selectDate}
    />
  ) : (
    <NewsPanel
      date={state.date}
      ticker={state.ticker}
      pricesReady={Boolean(state.session.data)}
      news={state.news}
      selectedNewsId={selectedNewsId}
      onSelectNews={setSelectedNewsId}
    />
  );
  const newsTitles = useMemo(
    () => new Map(newsItems.map((item) => [item.id, decodeEntities(item.title)])),
    [newsItems],
  );
  const backFromRun = () => {
    clearRunView();
    state.update({ run: null, case: null, tab: null });
  };
  // A quote links to its news card by item id, else by URL; a source outside this session's
  // news list has no card, and the quote then says nothing about a highlight.
  const newsByUrl = useMemo(
    () => new Map(newsItems.flatMap((item) => (item.url ? [[item.url, item.id] as const] : []))),
    [newsItems],
  );
  const selectQuote = (quote: AnswerQuote) => {
    setSelectedQuoteId(quote.id);
    const byId = quote.newsId && newsItems.some((item) => item.id === quote.newsId) ? quote.newsId : null;
    setSelectedNewsId(byId ?? (quote.url ? (newsByUrl.get(quote.url) ?? null) : null));
  };
  const attribution = (
    <AttributionPanel
      date={state.date}
      explainable={state.explainable}
      nonTradingDate={state.nonTradingDate || !summary.tradingDay}
      runParam={state.caseId ?? state.run}
      newestRun={newestRun}
      coverageStart={state.coverageStart}
      daySuggestions={daySuggestions}
      onPickDay={state.selectDate}
      // The panel does not wait for news: a slow headline list must not hold back the answer or the run button.
      isLoading={state.session.isLoading || state.sessionRuns.isLoading}
      ticker={state.ticker}
      modelLine={modelLine}
      tickerRuns={tickerRuns.data?.items ?? []}
      recentRuns={recentRuns.data?.items ?? []}
      onOpenRun={selectRun}
      session={state.session.data}
      move={summary.move}
      gap={summary.gap}
      intraday={summary.intraday}
      newsCount={state.news.data ? newsItems.length : null}
      overnightCount={overnightCount}
      inSessionCount={newsItems.length - overnightCount}
      candles={state.candles}
      cutoffDate={cutoffDate}
      avgCost={summary.avgCost}
      avgSeconds={summary.avgSeconds}
      onExplain={runs.request}
      onChangeModel={() => runs.setDialogOpen(true)}
      runError={runs.error}
      quotaLimit={quota.data?.per_caller?.limit}
      anonymousVisitor={
        quota.data?.tier === 'anonymous' && quota.data?.per_caller?.limit === 0 && !hasSavedBrowserKey
      }
      analysisCredential={resolvedModel?.credential ?? 'none'}
      freeRunsRemaining={quota.data?.per_caller?.remaining}
      quotaReached={runs.quotaReached}
      onAddKey={() => {
        setOpenOnOwnKey(true);
        runs.setDialogOpen(true);
      }}
      starting={runs.starting}
      retryable={runs.retryable}
      onUpdateRun={(run) => state.update({ run })}
      runningActions={runHeaderActions}
      runningContent={
        state.caseId && state.date ? (
          <ReplayContent
            key={state.caseId}
            caseId={state.caseId}
            ticker={state.ticker}
            date={state.date}
            tab={state.tab}
            onTabChange={(tab) => state.update({ tab: tab === 'answer' ? null : tab })}
            newsTitles={newsTitles}
            selectedQuoteId={selectedQuoteId}
            quoteLinked={Boolean(selectedQuoteId && selectedNewsId)}
            onSelectQuote={selectQuote}
            onBack={backFromRun}
            onHeaderActions={setRunHeaderActions}
          />
        ) : state.run && state.date ? (
          <RunContent
            key={state.run}
            runId={state.run}
            ticker={state.ticker}
            date={state.date}
            tab={state.tab}
            onTabChange={(tab) => state.update({ tab: tab === 'answer' ? null : tab })}
            newsTitles={newsTitles}
            selectedQuoteId={selectedQuoteId}
            quoteLinked={Boolean(selectedQuoteId && selectedNewsId)}
            onSelectQuote={selectQuote}
            onRunAgain={runs.retry}
            onBack={backFromRun}
            onRetry={(choice, cap) => void runs.start(choice, cap)}
            onChooseRun={() => runs.setDialogOpen(true)}
            retryChoice={runs.lastChoice}
            onHeaderActions={setRunHeaderActions}
          />
        ) : undefined
      }
    />
  );
  return (
    <>
      <WorkbenchLayout chart={chart} news={news} attribution={attribution} />
      <TickerPalette
        open={paletteOpen}
        onOpenChange={setPaletteOpen}
        tickers={state.tickerOptions}
        pricedTickers={rangeLocal.data?.tickers}
        recentRuns={state.recentRuns.data?.items ?? []}
        onSelectTicker={selectTicker}
        onSelectRun={selectRun}
      />
      <RunDialog
        open={runs.dialogOpen}
        onOpenChange={(open) => {
          runs.setDialogOpen(open);
          if (!open) setOpenOnOwnKey(false);
        }}
        ticker={state.ticker}
        date={state.date}
        estimate={summary.avgCost}
        averageSeconds={summary.avgSeconds}
        providers={state.catalog.data?.providers ?? []}
        quota={quota.data}
        defaultProvider={resolvedModel?.provider ?? ''}
        payer={runDefaults.payer ?? 'server'}
        openOnOwnKey={openOnOwnKey}
        costCapUsd={runDefaults.costCapUsd ?? 0.2}
        onSubmit={(choice) => void runs.start(choice)}
      />
    </>
  );
}
