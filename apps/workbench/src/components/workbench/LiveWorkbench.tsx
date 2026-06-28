/**
 * LiveWorkbench — BYOK production path.
 *
 * Real API calls only. No runtime imports from mock/demoCases or workflow-state.
 * View-model stubs and workspace mapping are delegated to workspace-adapter.ts.
 */
import { useState, useCallback, useEffect, useMemo } from 'react';
import {
  getTickers,
  getOhlcv,
  createLiveRun,
  getLiveRun,
  getLiveRunArtifacts,
  getWorkspace,
} from '../../api/client';
import type {
  ModelConfig,
  WorkspaceResponse,
  ArtifactResponse,
  OhlcvCandle,
  RunSummaryResponse,
} from '../../api/types';
import {
  mapEvidence,
  mapResult,
  mapSteps,
  mapNewsToEvidence,
  buildDemoCaseStub,
  makeCasesList,
} from './workspace-adapter';
import { getNews } from '../../api/client';
import type { EvidenceItem } from '../../mock/demoCases';
import WorkbenchHeader from './v4/WorkbenchHeader';
import MarketSessionPanel from './v4/MarketSessionPanel';
import SessionOverviewPanel from './v4/SessionOverviewPanel';
import EvidenceIntakePanel from './v4/EvidenceIntakePanel';
import ResultWorkspaceTabs from './v4/ResultWorkspaceTabs';
import './v4/workbench-v4.css';

type RunPhase = 'idle' | 'loading_tickers' | 'creating' | 'running' | 'terminal' | 'error';

const POLL_INTERVAL_MS = 2000;

export default function LiveWorkbench() {
  const [modelConfig, setModelConfig] = useState<ModelConfig | null>(null);
  const [tickers, setTickers] = useState<string[]>([]);
  const [selectedTicker, setSelectedTicker] = useState<string>('');
  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  const [candles, setCandles] = useState<OhlcvCandle[]>([]);
  const [phase, setPhase] = useState<RunPhase>('loading_tickers');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState<WorkspaceResponse | null>(null);
  const [artifacts, setArtifacts] = useState<ArtifactResponse[]>([]);
  const [newsPreviewEvidence, setNewsPreviewEvidence] = useState<EvidenceItem[]>([]);
  const [newsLoading, setNewsLoading] = useState(false);
  const [newsError, setNewsError] = useState<string | null>(null);

  // Scroll to top on mount — disable browser scroll restoration
  useEffect(() => {
    window.scrollTo(0, 0);
    document.documentElement.scrollTop = 0;
    document.body.scrollTop = 0;
    const mainEl = document.querySelector('.v4-page');
    if (mainEl) mainEl.scrollTop = 0;
  }, []);

  // Init: load tickers
  useEffect(() => {
    getTickers()
      .then((r: { symbols?: string[] }) => {
        const syms = r.symbols ?? [];
        setTickers(syms);
        if (syms.length > 0) {
          setSelectedTicker(syms[0]);
          setPhase('idle');
        }
      })
      .catch((err: Error) => {
        setErrorMessage(`Failed to load tickers: ${err.message}`);
        setPhase('error');
      });
  }, []);

  // When ticker changes, load candles
  useEffect(() => {
    if (!selectedTicker) return;
    getOhlcv(selectedTicker)
      .then((r: { candles?: OhlcvCandle[] }) => {
        const c = r.candles ?? [];
        setCandles(c);
        if (c.length > 0) {
          setSelectedDate(c[Math.floor(c.length / 2)].date);
        }
      })
      .catch((err: Error) => {
        setErrorMessage(`Failed to load chart data: ${err.message}`);
      });
  }, [selectedTicker]);

  // Fetch news preview when ticker or date changes
  useEffect(() => {
    if (!selectedTicker || !selectedDate) {
      setNewsPreviewEvidence([]);
      setNewsError(null);
      return;
    }

    let cancelled = false;
    setNewsLoading(true);
    setNewsError(null);

    getNews(selectedTicker, selectedDate, 3)
      .then((r) => {
        if (cancelled) return;
        const mapped = mapNewsToEvidence(r.items ?? [], selectedDate);
        setNewsPreviewEvidence(mapped);
        setNewsLoading(false);
      })
      .catch((err: Error) => {
        if (cancelled) return;
        setNewsError(`News preview failed: ${err.message}`);
        setNewsPreviewEvidence([]);
        setNewsLoading(false);
      });

    return () => { cancelled = true; };
  }, [selectedTicker, selectedDate]);

  const demoCase = useMemo(
    () => buildDemoCaseStub(selectedTicker || '---', candles),
    [selectedTicker, candles],
  );

  const cases = useMemo(() => makeCasesList(tickers), [tickers]);

  const canRun =
    selectedTicker !== '' &&
    selectedDate !== null &&
    modelConfig !== null &&
    phase === 'idle';

  // Map workspace to v4 shapes
  const v4Result = useMemo(() => (workspace ? mapResult(workspace) : null), [workspace]);
  const v4Evidence = useMemo(
    () => (workspace ? mapEvidence(workspace.evidence ?? []) : []),
    [workspace],
  );

  // Display evidence: workspace after terminal, else news preview
  const hasWorkspaceResult = phase === 'terminal' && workspace !== null;
  const displayEvidence = useMemo(() => {
    if (hasWorkspaceResult) return v4Evidence;
    return newsPreviewEvidence;
  }, [hasWorkspaceResult, v4Evidence, newsPreviewEvidence]);
  const displayMode: 'preview' | 'retrieved' = hasWorkspaceResult ? 'retrieved' : 'preview';
  const v4Steps = useMemo(
    () => (workspace ? mapSteps(workspace.stages ?? []) : []),
    [workspace],
  );
  const referencedIds = useMemo(
    () =>
      new Set((workspace?.result?.causes ?? []).flatMap((c) => c.evidence_ids ?? [])),
    [workspace],
  );

  const handleRun = useCallback(async () => {
    if (!canRun || !selectedTicker || !selectedDate || !modelConfig) return;
    setPhase('creating');
    setErrorMessage(null);
    setWorkspace(null);
    setArtifacts([]);

    try {
      const resp = await createLiveRun({
        ticker: selectedTicker,
        trade_date: selectedDate,
        model: modelConfig,
      });
      setRunId(resp.run_id);
      setPhase('running');
    } catch (err) {
      setPhase('error');
      setErrorMessage(
        err instanceof Error ? `Run creation failed: ${err.message}` : 'Failed to create run',
      );
    }
  }, [canRun, selectedTicker, selectedDate, modelConfig]);

  // Poll for run status, then fetch workspace
  useEffect(() => {
    if (phase !== 'running' || !runId) return;

    const poll = async () => {
      try {
        const summary: RunSummaryResponse = await getLiveRun(runId);
        const terminal = [
          'SUCCEEDED', 'PARTIAL', 'INSUFFICIENT',
          'FAILED_SYSTEM', 'FAILED_REQUEST', 'CANCELLED',
        ];
        if (terminal.includes(summary.status)) {
          setPhase('terminal');
          try {
            const ws = await getWorkspace(runId);
            setWorkspace(ws);
          } catch (err) {
            setErrorMessage(
              err instanceof Error
                ? `Workspace fetch failed: ${err.message}`
                : 'Failed to load workspace',
            );
          }
          try {
            const arts = await getLiveRunArtifacts(runId);
            setArtifacts(arts);
          } catch {
            /* artifacts are optional */
          }
          return;
        }
      } catch (err) {
        setErrorMessage(
          err instanceof Error ? `Polling error: ${err.message}` : 'Polling failed',
        );
      }
    };

    poll();
    const interval = setInterval(poll, POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [phase, runId]);

  const handleTickerChange = useCallback((id: string) => {
    setSelectedTicker(id);
    setPhase('idle');
    setWorkspace(null);
    setRunId(null);
    setErrorMessage(null);
  }, []);

  const showLoading = phase === 'loading_tickers';
  const showError = phase === 'error' && errorMessage !== null;
  const showRunning = phase === 'running' || phase === 'creating';

  return (
    <div className="v4-page">
      <div className="v4-shell">
      <WorkbenchHeader
        demoCase={demoCase as any}
        cases={cases as any}
        selectedDate={selectedDate}
        phase={showRunning ? 'running' : 'idle'}
        canRun={canRun}
        onCaseChange={handleTickerChange as any}
        onRun={handleRun}
        onModelReady={setModelConfig}
        onModelClear={() => setModelConfig(null)}
      />

      {showLoading && (
        <div className="v4-state-banner">Loading market data…</div>
      )}

      {showError && (
        <div className="v4-state-banner v4-state-banner--error" role="alert">
          {errorMessage}
        </div>
      )}

      <section className="v4-market-composite">
        <MarketSessionPanel
          demoCase={demoCase as any}
          selectedDate={selectedDate}
          onSelectDate={setSelectedDate}
        />
        <div className="v4-market-session-col">
          <SessionOverviewPanel
            demoCase={demoCase as any}
            selectedDate={selectedDate}
            canRun={canRun}
          />
        </div>
      </section>

      <EvidenceIntakePanel
        evidence={displayEvidence}
        referencedIds={referencedIds}
        completed={phase === 'terminal'}
        selectedDate={selectedDate}
        mode={displayMode}
        loading={newsLoading}
        error={newsError}
      />

      <ResultWorkspaceTabs
        result={v4Result}
        evidence={v4Evidence}
        referencedIds={referencedIds}
        steps={v4Steps}
        phase={showRunning ? 'running' : phase === 'terminal' ? 'completed' : 'idle'}
        errorMessage={errorMessage}
        artifacts={artifacts}
        runtimeMs={workspace?.runtime_ms ?? undefined}
      />
      </div>
    </div>
  );
}
