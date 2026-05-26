import type { RunSummaryResponse, RuntimeStatus } from '../api/types';

export const TERMINAL_STATUSES = [
  'SUCCEEDED',
  'PARTIAL',
  'INSUFFICIENT',
  'FAILED_SYSTEM',
  'FAILED_REQUEST',
] as const satisfies readonly RuntimeStatus[];

export type TerminalStatus = (typeof TERMINAL_STATUSES)[number];

export interface SelectedContext {
  ticker: string | null;
  tradeDate: string | null;
}

export interface WorkbenchState {
  selected: SelectedContext;
  activeRunId: string | null;
  runStatus: RuntimeStatus | null;
  lastEventSeq: number;
  lastCompletedNode: string | null;
  predictedNextNode: string | null;
}

export type RunSummaryInput = Pick<
  RunSummaryResponse,
  'run_id' | 'status' | 'ticker' | 'trade_date' | 'last_completed_node' | 'predicted_next_node'
>;

export function createInitialState(): WorkbenchState {
  return {
    selected: {
      ticker: null,
      tradeDate: null,
    },
    activeRunId: null,
    runStatus: null,
    lastEventSeq: 0,
    lastCompletedNode: null,
    predictedNextNode: null,
  };
}

export function selectMarketContext(
  state: WorkbenchState,
  selected: Partial<SelectedContext>,
): WorkbenchState {
  return {
    ...state,
    selected: {
      ticker: selected.ticker ?? state.selected.ticker,
      tradeDate: selected.tradeDate ?? state.selected.tradeDate,
    },
  };
}

export function reduceRunSummary(state: WorkbenchState, summary: RunSummaryInput): WorkbenchState {
  return {
    ...state,
    selected: {
      ticker: summary.ticker ?? state.selected.ticker,
      tradeDate: summary.trade_date ?? state.selected.tradeDate,
    },
    activeRunId: summary.run_id,
    runStatus: summary.status,
    lastCompletedNode: summary.last_completed_node ?? state.lastCompletedNode,
    predictedNextNode: summary.predicted_next_node ?? state.predictedNextNode,
  };
}

export function reduceLastEventSeq(state: WorkbenchState, eventSeq: number): WorkbenchState {
  return {
    ...state,
    lastEventSeq: Math.max(state.lastEventSeq, eventSeq),
  };
}

export function isTerminalStatus(status: RuntimeStatus | null): status is TerminalStatus {
  return status !== null && TERMINAL_STATUSES.includes(status as TerminalStatus);
}

export function isPollingRequired(state: WorkbenchState): boolean {
  return state.activeRunId !== null && state.runStatus !== null && !isTerminalStatus(state.runStatus);
}
