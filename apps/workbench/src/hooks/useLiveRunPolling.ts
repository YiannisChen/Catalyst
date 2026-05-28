import { useEffect, useRef, useCallback } from 'react';
import { getLiveRun, getLiveRunEvents } from '../api/client';
import { isPollingRequired } from '../state/workbench-state';
import type { WorkbenchState, RunSummaryInput } from '../state/workbench-state';
import type { RunEventResponse } from '../api/types';

const POLL_INTERVAL = 1500; // 1.5 seconds

interface Callbacks {
  onSummaryUpdate: (summary: RunSummaryInput) => void;
  onEventsUpdate: (events: RunEventResponse[]) => void;
}

export function useLiveRunPolling(state: WorkbenchState, callbacks: Callbacks) {
  const intervalRef = useRef<NodeJS.Timeout | null>(null);
  const onSummaryUpdateRef = useRef(callbacks.onSummaryUpdate);
  const onEventsUpdateRef = useRef(callbacks.onEventsUpdate);

  onSummaryUpdateRef.current = callbacks.onSummaryUpdate;
  onEventsUpdateRef.current = callbacks.onEventsUpdate;

  const poll = useCallback(async () => {
    if (!state.activeRunId) return;

    try {
      const summary = await getLiveRun(state.activeRunId);
      onSummaryUpdateRef.current({
        run_id: summary.run_id,
        status: summary.status,
        ticker: summary.ticker || null,
        trade_date: summary.trade_date || null,
        last_completed_node: summary.last_completed_node || null,
        predicted_next_node: summary.predicted_next_node || null,
      });

      const events = await getLiveRunEvents(state.activeRunId, state.lastEventSeq);
      if (events.length > 0) {
        onEventsUpdateRef.current(events);
      }
    } catch (err) {
      console.error('Polling failed:', err);
    }
  }, [state.activeRunId, state.lastEventSeq]);

  useEffect(() => {
    if (!isPollingRequired(state)) {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
      return;
    }

    poll();
    intervalRef.current = setInterval(poll, POLL_INTERVAL);

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [state, poll]);
}
