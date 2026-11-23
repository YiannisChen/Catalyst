import { useEffect, useMemo, useState } from 'react';
import { streamRun, type PublicRunEvent } from '@/api/sse';
import { reduceRunEvents } from './progress';

export type RunStreamStatus = 'idle' | 'connecting' | 'streaming' | 'error';

export function useRunStream(runId: string | null | undefined) {
  const [events, setEvents] = useState<PublicRunEvent[]>([]);
  const [status, setStatus] = useState<RunStreamStatus>(runId ? 'connecting' : 'idle');
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    setEvents([]);
    setError(null);
    if (!runId) {
      setStatus('idle');
      return;
    }
    const controller = new AbortController();
    setStatus('connecting');
    void streamRun(
      runId,
      (event) => {
        setEvents((current) => {
          const bySequence = new Map(current.map((item) => [item.sequence, item]));
          if (!bySequence.has(event.sequence)) bySequence.set(event.sequence, event);
          return [...bySequence.values()].sort((a, b) => a.sequence - b.sequence);
        });
        setStatus('streaming');
      },
      controller.signal,
      () => setStatus('connecting'),
    ).catch((cause: unknown) => {
      if (controller.signal.aborted) return;
      setError(cause instanceof Error ? cause : new Error('The run stream stopped unexpectedly.'));
      setStatus('error');
    });
    return () => controller.abort();
  }, [runId]);

  const progress = useMemo(() => reduceRunEvents(events), [events]);
  return { events, progress, status, error };
}
