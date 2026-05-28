import { useReducer, useState, useCallback, useEffect } from 'react';
import StockSelector from './components/context/StockSelector';
import ModelSelector from './components/context/ModelSelector';
import CandlestickChart from './components/chart/CandlestickChart';
import PipelinePanel from './components/runtime/PipelinePanel';
import AttributionSummary from './components/attribution/AttributionSummary';
import NewsPanel from './components/context/NewsPanel';
import FundamentalsCard from './components/context/FundamentalsCard';
import { getTickers, getRangeLocal, createLiveRun, getLiveRun, getLiveRunArtifacts } from './api/client';
import { useLiveRunPolling } from './hooks/useLiveRunPolling';
import {
  createInitialState,
  selectMarketContext,
  reduceRunSummary,
  reduceLastEventSeq,
  isTerminalStatus,
  TERMINAL_STATUSES,
} from './state/workbench-state';
import type { WorkbenchState, RunSummaryInput } from './state/workbench-state';
import type { RunEventResponse, ArtifactResponse } from './api/types';
import {
  MOCK_SUMMARY_SUCCEEDED,
  MOCK_SUMMARY_FAILED,
  MOCK_EVENTS,
  MOCK_ARTIFACTS,
} from './dev/mockRunData';
import './App.css';

function workbenchReducer(state: WorkbenchState, action: any): WorkbenchState {
  switch (action.type) {
    case 'SELECT_MARKET':
      return selectMarketContext(state, action.payload);
    case 'UPDATE_RUN_SUMMARY':
      return reduceRunSummary(state, action.payload);
    case 'UPDATE_EVENT_SEQ':
      return reduceLastEventSeq(state, action.payload);
    default:
      return state;
  }
}

function App() {
  const [state, dispatch] = useReducer(workbenchReducer, undefined, createInitialState);
  const [activeTickers, setActiveTickers] = useState<string[]>([]);
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [events, setEvents] = useState<RunEventResponse[]>([]);
  const [artifacts, setArtifacts] = useState<ArtifactResponse[]>([]);
  const [hoveredData, setHoveredData] = useState<any>(null);
  const [creatingRun, setCreatingRun] = useState(false);
  const [runSummary, setRunSummary] = useState<any>(null);
  const [marketContextOpen, setMarketContextOpen] = useState(true);

  // Initialize tickers and default date
  useEffect(() => {
    getTickers()
      .then((res) => {
        const tickers = res.symbols || [];
        setActiveTickers(tickers);
        if (tickers.length > 0 && !state.selected.ticker) {
          dispatch({
            type: 'SELECT_MARKET',
            payload: { ticker: tickers[0] },
          });
        }
      })
      .catch((err) => {
        console.error('Failed to fetch tickers:', err);
      });

    // Set default trade date from data range
    if (!state.selected.tradeDate) {
      getRangeLocal()
        .then((range) => {
          if (range.max_date) {
            dispatch({
              type: 'SELECT_MARKET',
              payload: { tradeDate: range.max_date },
            });
          }
        })
        .catch((err) => {
          console.error('Failed to fetch date range:', err);
        });
    }
  }, []);

  // Dev mock injection: Ctrl+Shift+M = success, Ctrl+Shift+F = failure, Ctrl+Shift+C = clear
  useEffect(() => {
    if (import.meta.env.PROD) return;
    const handler = (e: KeyboardEvent) => {
      if (!e.ctrlKey || !e.shiftKey) return;
      if (e.key === 'M' || e.key === 'm') {
        e.preventDefault();
        console.log('[DEV] Injecting mock SUCCEEDED run');
        dispatch({ type: 'UPDATE_RUN_SUMMARY', payload: MOCK_SUMMARY_SUCCEEDED });
        setRunSummary(MOCK_SUMMARY_SUCCEEDED);
        setEvents(MOCK_EVENTS);
        setArtifacts(MOCK_ARTIFACTS);
        setMarketContextOpen(false);
      } else if (e.key === 'F' || e.key === 'f') {
        e.preventDefault();
        console.log('[DEV] Injecting mock FAILED_SYSTEM run');
        dispatch({ type: 'UPDATE_RUN_SUMMARY', payload: MOCK_SUMMARY_FAILED });
        setRunSummary(MOCK_SUMMARY_FAILED);
        setEvents(MOCK_EVENTS.slice(0, 3));
        setArtifacts([]);
        setMarketContextOpen(false);
      } else if (e.key === 'C' || e.key === 'c') {
        e.preventDefault();
        console.log('[DEV] Clearing mock data');
        setRunSummary(null);
        setEvents([]);
        setArtifacts([]);
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []);

  // Polling hook
  useLiveRunPolling(state, {
    onSummaryUpdate: (summary: RunSummaryInput) => {
      dispatch({ type: 'UPDATE_RUN_SUMMARY', payload: summary });
      setRunSummary(summary);

      // Fetch artifacts when terminal
      if (isTerminalStatus(summary.status)) {
        getLiveRunArtifacts(summary.run_id)
          .then(setArtifacts)
          .catch((err) => console.error('Failed to fetch artifacts:', err));
      }
    },
    onEventsUpdate: (newEvents: RunEventResponse[]) => {
      setEvents((prev) => [...prev, ...newEvents]);
      if (newEvents.length > 0) {
        dispatch({
          type: 'UPDATE_EVENT_SEQ',
          payload: newEvents[newEvents.length - 1].event_seq,
        });
      }
    },
  });

  const handleCreateRun = useCallback(async () => {
    if (!state.selected.ticker || !state.selected.tradeDate) return;

    setCreatingRun(true);
    setEvents([]);
    setArtifacts([]);
    setMarketContextOpen(false);

    try {
      const response = await createLiveRun({
        ticker: state.selected.ticker,
        trade_date: state.selected.tradeDate,
        model_id: selectedModel || undefined,
      });

      const summary = await getLiveRun(response.run_id);
      dispatch({
        type: 'UPDATE_RUN_SUMMARY',
        payload: {
          run_id: summary.run_id,
          status: summary.status,
          ticker: summary.ticker || null,
          trade_date: summary.trade_date || null,
          last_completed_node: summary.last_completed_node || null,
          predicted_next_node: summary.predicted_next_node || null,
        },
      });
      setRunSummary(summary);
    } catch (err) {
      console.error('Failed to create run:', err);
    } finally {
      setCreatingRun(false);
    }
  }, [state.selected.ticker, state.selected.tradeDate, selectedModel]);

  const isTerminal = state.runStatus ? (TERMINAL_STATUSES as readonly string[]).includes(state.runStatus) : false;
  const isRunning = state.runStatus === 'QUEUED' || state.runStatus === 'RUNNING';

  return (
    <div className="app-container">
      {/* ===== Sticky Zone: header + controls + chart ===== */}
      <div className="sticky-zone">
        <header className="app-header">
          <h1 className="app-title">Catalyst — Attribution Workbench</h1>
        </header>

        <div className="control-bar">
          <div className="control-bar-group">
            <label className="control-label">Ticker:</label>
            <StockSelector
              activeTickers={activeTickers}
              selectedSymbol={state.selected.ticker || ''}
              onSelect={(ticker) =>
                dispatch({
                  type: 'SELECT_MARKET',
                  payload: { ticker },
                })
              }
            />
          </div>

          <div className="control-bar-group">
            <label className="control-label">Date:</label>
            <input
              type="date"
              value={state.selected.tradeDate || ''}
              onChange={(e) =>
                dispatch({
                  type: 'SELECT_MARKET',
                  payload: { tradeDate: e.target.value },
                })
              }
              className="control-input"
            />
          </div>

          <div className="control-bar-group">
            <label className="control-label">Model:</label>
            <ModelSelector
              selectedModel={selectedModel}
              onSelect={setSelectedModel}
            />
          </div>

          <button
            className="control-button"
            onClick={handleCreateRun}
            disabled={
              !state.selected.ticker ||
              !state.selected.tradeDate ||
              creatingRun ||
              isRunning
            }
          >
            {creatingRun ? 'Creating...' : isRunning ? 'Running...' : 'Run Attribution'}
          </button>
        </div>

        <div className="chart-row">
          <div className="chart-section">
            <CandlestickChart
              symbol={state.selected.ticker || ''}
              onHover={(_date, data) => {
                setHoveredData(data || null);
              }}
              onDayClick={(date) =>
                dispatch({
                  type: 'SELECT_MARKET',
                  payload: { tradeDate: date },
                })
              }
            />
          </div>

          <div className="day-context">
            <div className="context-header">
              <span className="context-ticker">{state.selected.ticker || '---'}</span>
              {state.selected.tradeDate && (
                <span className="context-date">{state.selected.tradeDate}</span>
              )}
              {state.runStatus && (
                <span className={`context-status-chip ${state.runStatus.toLowerCase()}`}>
                  {state.runStatus.replace('_', ' ')}
                </span>
              )}
            </div>

            {hoveredData ? (
              <div className="context-ohlc">
                <div className="ohlc-grid">
                  <div className="ohlc-cell">
                    <span className="ohlc-label">Date</span>
                    <span className="ohlc-value">{hoveredData.date}</span>
                  </div>
                  <div className="ohlc-cell">
                    <span className="ohlc-label">Open</span>
                    <span className="ohlc-value">${hoveredData.open.toFixed(2)}</span>
                  </div>
                  <div className="ohlc-cell">
                    <span className="ohlc-label">High</span>
                    <span className="ohlc-value">${hoveredData.high.toFixed(2)}</span>
                  </div>
                  <div className="ohlc-cell">
                    <span className="ohlc-label">Low</span>
                    <span className="ohlc-value">${hoveredData.low.toFixed(2)}</span>
                  </div>
                  <div className="ohlc-cell">
                    <span className="ohlc-label">Close</span>
                    <span className="ohlc-value">${hoveredData.close.toFixed(2)}</span>
                  </div>
                  <div className="ohlc-cell">
                    <span className="ohlc-label">Change</span>
                    <span className={`ohlc-value ${hoveredData.change >= 0 ? 'up' : 'down'}`}>
                      {hoveredData.change >= 0 ? '+' : ''}{hoveredData.change.toFixed(2)}%
                    </span>
                  </div>
                </div>
              </div>
            ) : (
              <div className="context-hint">Hover over chart to see price data</div>
            )}
          </div>
        </div>
      </div>

      {/* ===== Scroll Zone: market context + runtime + summary ===== */}
      <div className="scroll-zone">
        {/* Market Context — collapsible News + Fundamentals */}
        {state.selected.ticker && state.selected.tradeDate && (
          <div className="market-context-section">
            <div
              className="market-context-header"
              onClick={() => setMarketContextOpen((v) => !v)}
            >
              <span className="market-context-title">
                {marketContextOpen ? '▾' : '▸'} Market Context
              </span>
              <span className="market-context-toggle">
                {marketContextOpen ? 'collapse' : 'expand'}
              </span>
            </div>
            <div className={`market-context-body ${marketContextOpen ? '' : 'collapsed'}`}>
              <div className="market-context-col">
                <NewsPanel ticker={state.selected.ticker} tradeDate={state.selected.tradeDate} />
              </div>
              <div className="market-context-col">
                <FundamentalsCard ticker={state.selected.ticker} tradeDate={state.selected.tradeDate} />
              </div>
            </div>
          </div>
        )}

        {/* Attribution Runtime */}
        <div className="attribution-runtime-section">
          <PipelinePanel
            summary={runSummary}
            events={events}
            runId={state.activeRunId}
            artifacts={artifacts}
            ticker={state.selected.ticker}
            tradeDate={state.selected.tradeDate}
          />
        </div>

        {/* Run Summary Footer — only when terminal */}
        {isTerminal && (
          <div className="run-summary-footer">
            <AttributionSummary
              summary={runSummary}
              onRetry={(newRunId) => {
                if (newRunId) {
                  setEvents([]);
                  setArtifacts([]);
                  dispatch({
                    type: 'UPDATE_RUN_SUMMARY',
                    payload: {
                      run_id: newRunId,
                      status: 'QUEUED',
                      ticker: state.selected.ticker,
                      trade_date: state.selected.tradeDate,
                      last_completed_node: null,
                      predicted_next_node: 'miner',
                    },
                  });
                  setRunSummary(null);
                }
              }}
            />
          </div>
        )}
      </div>
    </div>
  );
}

export default App;
