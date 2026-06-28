export type WorkflowPhase = 'idle' | 'running' | 'completed' | 'error';

export interface WorkflowState {
  selectedDate: string | null;
  phase: WorkflowPhase;
  completedStepCount: number;
  errorMessage: string | null;
}

export type WorkflowAction =
  | { type: 'RESET_SCENARIO'; date: string | null }
  | { type: 'SELECT_DATE'; date: string }
  | { type: 'START_RUN' }
  | { type: 'ADVANCE_STEP' }
  | { type: 'COMPLETE_RUN' }
  | { type: 'FAIL_RUN'; message: string };

export function createWorkflowState(selectedDate: string | null): WorkflowState {
  return {
    selectedDate,
    phase: 'idle',
    completedStepCount: 0,
    errorMessage: null,
  };
}

export function canRunAttribution(state: WorkflowState): boolean {
  return state.selectedDate !== null && state.phase !== 'running';
}

export function workflowReducer(state: WorkflowState, action: WorkflowAction): WorkflowState {
  switch (action.type) {
    case 'RESET_SCENARIO':
      return createWorkflowState(action.date);
    case 'SELECT_DATE':
      return createWorkflowState(action.date);
    case 'START_RUN':
      return state.selectedDate
        ? { ...state, phase: 'running', completedStepCount: 0, errorMessage: null }
        : state;
    case 'ADVANCE_STEP':
      return state.phase === 'running'
        ? { ...state, completedStepCount: state.completedStepCount + 1 }
        : state;
    case 'COMPLETE_RUN':
      return state.phase === 'running'
        ? { ...state, phase: 'completed', completedStepCount: 9 }
        : state;
    case 'FAIL_RUN':
      return { ...state, phase: 'error', errorMessage: action.message };
    default:
      return state;
  }
}
