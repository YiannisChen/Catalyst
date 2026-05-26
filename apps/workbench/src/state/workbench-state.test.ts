import { describe, expect, it } from 'vitest';

import { createInitialState, isPollingRequired, reduceRunSummary } from './workbench-state';
import type { RuntimeStatus } from '../api/types';

describe('workbench state', () => {
  it('transitions queued->running->terminal and stops polling', () => {
    const s0 = createInitialState();
    const s1 = reduceRunSummary(s0, { status: 'QUEUED', run_id: 'r1' });
    const s2 = reduceRunSummary(s1, { status: 'RUNNING', run_id: 'r1' });
    const s3 = reduceRunSummary(s2, { status: 'SUCCEEDED', run_id: 'r1' });

    expect(isPollingRequired(s3)).toBe(false);
  });

  it('stops polling for all terminal statuses', () => {
    const terminalStatuses = [
      'SUCCEEDED',
      'PARTIAL',
      'INSUFFICIENT',
      'FAILED_SYSTEM',
      'FAILED_REQUEST',
    ] as const satisfies readonly RuntimeStatus[];

    expect(terminalStatuses).toHaveLength(5);

    for (const status of terminalStatuses) {
      const state = reduceRunSummary(createInitialState(), { status, run_id: 'r1' });

      expect(isPollingRequired(state)).toBe(false);
    }
  });
});
