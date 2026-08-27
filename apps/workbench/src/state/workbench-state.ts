import type { RunSummaryResponse, RuntimeStatus } from '../api/types';

export const TERMINAL_STATUSES = [
  'SUCCEEDED',
  'PARTIAL',
  'INSUFFICIENT',
  'FAILED_SYSTEM',
  'FAILED_REQUEST',
  'CANCELLED',
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

// ── V1.1 SSE reducer (M6-10) ────────────────────────────────────────────────
// The frontend derives display state only from persisted public events and
// authoritative RunDTOs. It must never infer materiality, independence,
// support, status, primary cause, causal role, claim links, or assurance from
// raw values or event order (no-fabrication rule, Final TSD §21).

import type { PublicRunEvent, RunDTO, RunEventType, RunLifecycleStatus } from '../api/types'

export interface V1WorkbenchState {
  runId: string | null
  lifecycleStatus: RunLifecycleStatus | null
  attributionStatus: string | null
  attributionType: string | null
  lastAppliedSequence: number
  gapDetected: boolean
  appliedKeys: Set<string>
  deltas: string[]
  provisionalInvalidated: boolean
  terminalEventType: RunEventType | null
  finalResultStatus: string | null
  limitations: string[]
  gaps: string[]
}

export function createV1State(): V1WorkbenchState {
  return {
    runId: null,
    lifecycleStatus: null,
    attributionStatus: null,
    attributionType: null,
    lastAppliedSequence: 0,
    gapDetected: false,
    appliedKeys: new Set<string>(),
    deltas: [],
    provisionalInvalidated: false,
    terminalEventType: null,
    finalResultStatus: null,
    limitations: [],
    gaps: [],
  }
}

export function reduceEvent(state: V1WorkbenchState, event: PublicRunEvent): V1WorkbenchState {
  if (state.runId !== null && event.run_id !== state.runId) {
    return state // per-run reducer; foreign events are ignored
  }
  const key = `${event.run_id}:${event.sequence}`
  if (state.appliedKeys.has(key)) {
    return state // dedup by (run_id, sequence); delivery is at least once
  }
  const next: V1WorkbenchState = {
    ...state,
    runId: state.runId ?? event.run_id,
    appliedKeys: new Set(state.appliedKeys),
    deltas: [...state.deltas],
    limitations: [...state.limitations],
    gaps: [...state.gaps],
  }
  next.appliedKeys.add(key)
  if (event.sequence > state.lastAppliedSequence + 1) {
    next.gapDetected = true
  }
  next.lastAppliedSequence = Math.max(state.lastAppliedSequence, event.sequence)

  const payload = event.payload ?? {}
  switch (event.event_type) {
    case 'run.accepted':
      next.lifecycleStatus = 'ACCEPTED'
      break
    case 'answer.delta':
      if (typeof payload.delta_text === 'string') {
        next.deltas.push(payload.delta_text)
      }
      break
    case 'assurance.completed':
      if (payload.provisional_invalidated === true || payload.valid === false) {
        next.provisionalInvalidated = true
      }
      if (typeof payload.final_result_status === 'string') {
        next.finalResultStatus = payload.final_result_status
      }
      break
    case 'run.completed':
      next.lifecycleStatus = 'COMPLETED'
      next.terminalEventType = event.event_type
      if (typeof payload.result_status === 'string') {
        next.finalResultStatus = payload.result_status
      }
      break
    case 'run.failed':
      next.lifecycleStatus = 'FAILED'
      next.terminalEventType = event.event_type
      break
    case 'run.cancelled':
      next.lifecycleStatus = 'CANCELLED'
      next.terminalEventType = event.event_type
      break
    default:
      break
  }
  return next
}

export function reduceEventBatch(state: V1WorkbenchState, events: PublicRunEvent[]): V1WorkbenchState {
  let next = state
  for (const event of events) {
    next = reduceEvent(next, event)
  }
  return next
}

export type RunDtoWithProjection = RunDTO & {
  limitations?: string[]
  gaps?: string[]
}

export function reduceRunDto(state: V1WorkbenchState, dto: RunDtoWithProjection): V1WorkbenchState {
  return {
    ...state,
    runId: dto.run_id,
    lifecycleStatus: dto.lifecycle_status,
    attributionStatus: dto.attribution_status ?? null,
    attributionType: dto.attribution_type ?? null,
    limitations: Array.isArray(dto.limitations) ? dto.limitations : state.limitations,
    gaps: Array.isArray(dto.gaps) ? dto.gaps : state.gaps,
  }
}

export interface WorkbenchDisplayState {
  lifecycleStatus: RunLifecycleStatus | null
  attributionStatus: string | null
  attributionType: string | null
  answerText: string
  provisional: boolean
  provisionalInvalidated: boolean
  terminalEventType: RunEventType | null
  finalResultStatus: string | null
  limitations: string[]
  gaps: string[]
  noAcceptedCause: boolean
  attributionBadgeVisible: boolean
}

export function deriveDisplayState(state: V1WorkbenchState): WorkbenchDisplayState {
  const terminal = state.terminalEventType !== null
  return {
    lifecycleStatus: state.lifecycleStatus,
    attributionStatus: state.attributionStatus,
    attributionType: state.attributionType,
    answerText: state.deltas.join(''),
    provisional: !terminal && !state.provisionalInvalidated,
    provisionalInvalidated: state.provisionalInvalidated,
    terminalEventType: state.terminalEventType,
    finalResultStatus: state.finalResultStatus,
    limitations: state.limitations,
    gaps: state.gaps,
    noAcceptedCause: state.attributionStatus === 'ABSTAIN',
    attributionBadgeVisible:
      state.attributionStatus === 'PARTIAL' || state.attributionStatus === 'SUFFICIENT',
  }
}
