import { describe, expect, it } from 'vitest';

import { pollIntervalMs, shouldPoll } from './polling';
import { createInitialState, reduceRunSummary } from './workbench-state';

describe('polling controller', () => {
  it('polls active runs at a deterministic interval', () => {
    const state = reduceRunSummary(createInitialState(), { status: 'RUNNING', run_id: 'r1' });

    expect(shouldPoll(state)).toBe(true);
    expect(pollIntervalMs(state)).toBe(1500);
  });

  it('does not poll without an active run id', () => {
    expect(shouldPoll(createInitialState())).toBe(false);
  });
});
