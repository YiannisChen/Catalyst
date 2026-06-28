import assert from 'node:assert/strict';
import test from 'node:test';

import {
  canRunAttribution,
  createWorkflowState,
  workflowReducer,
} from './workflow-state.ts';

test('cannot run before a candle date is selected', () => {
  const state = createWorkflowState(null);
  assert.equal(canRunAttribution(state), false);
  assert.deepEqual(workflowReducer(state, { type: 'START_RUN' }), state);
});

test('selecting another candle clears a completed result', () => {
  let state = createWorkflowState('2025-09-08');
  state = workflowReducer(state, { type: 'START_RUN' });
  state = workflowReducer(state, { type: 'COMPLETE_RUN' });
  state = workflowReducer(state, { type: 'SELECT_DATE', date: '2025-09-05' });

  assert.equal(state.selectedDate, '2025-09-05');
  assert.equal(state.phase, 'idle');
  assert.equal(state.completedStepCount, 0);
});

test('advances a running workflow before exposing completion', () => {
  let state = createWorkflowState('2025-09-08');
  state = workflowReducer(state, { type: 'START_RUN' });
  state = workflowReducer(state, { type: 'ADVANCE_STEP' });

  assert.equal(state.phase, 'running');
  assert.equal(state.completedStepCount, 1);

  state = workflowReducer(state, { type: 'COMPLETE_RUN' });
  assert.equal(state.phase, 'completed');
});
