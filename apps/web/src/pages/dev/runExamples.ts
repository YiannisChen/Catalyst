import type { PublicRunEvent } from '@/api/sse';
import nowStream from '../../../../../contracts/fixtures/events-t3-now.jsonl?raw';

/** The recorded NOW 2026-04-10 run (fixture events-t3-now), cut at different points for the gallery. */
const recorded = nowStream
  .split(/\r?\n/)
  .filter(Boolean)
  .map((line) => JSON.parse(line) as PublicRunEvent);

function terminal(eventType: PublicRunEvent['event_type'], payload: Record<string, unknown>): PublicRunEvent {
  const last = recorded[4]!;
  return {
    ...last,
    event_type: eventType,
    sequence: last.sequence + 1,
    emitted_at: '2026-10-09T16:15:10.000000Z',
    payload: payload as PublicRunEvent['payload'],
    artifact_refs: [],
  };
}

export const runExamples = {
  afterFacts: recorded.slice(0, 3),
  midAssessment: recorded.slice(0, 5),
  done: recorded,
  // Sample terminal events appended to the recorded start; no recorded failed or cancelled stream exists.
  failed: [
    ...recorded.slice(0, 5),
    terminal('run.failed', {
      failure_code: 'provider_error',
      safe_message: 'The model provider could not complete the request.',
      retryable: true,
      provisional_output_invalidated: true,
      stage: 'ANSWER',
    }),
  ],
  cancelled: [
    ...recorded.slice(0, 5),
    terminal('run.cancelled', {
      cancellation_reason: 'user_requested',
      acknowledgement_mode: 'immediate',
      provisional_output_invalidated: true,
      stage: 'ANALYST',
    }),
  ],
};
