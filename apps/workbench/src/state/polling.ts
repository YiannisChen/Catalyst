import { isPollingRequired, type WorkbenchState } from './workbench-state';

const ACTIVE_RUN_POLL_INTERVAL_MS = 1500;

export function shouldPoll(state: WorkbenchState): boolean {
  return isPollingRequired(state);
}

export function pollIntervalMs(state: WorkbenchState): number | null {
  return shouldPoll(state) ? ACTIVE_RUN_POLL_INTERVAL_MS : null;
}
