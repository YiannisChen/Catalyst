import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createV1State, reduceEvent, reduceEventBatch, deriveDisplayState } from './workbench-state.ts'
import { buildStreamUrl, parseSseFrame } from '../api/client.ts'
import type { PublicRunEvent } from '../api/types.ts'

function event(seq: number, eventType: PublicRunEvent['event_type'], payload: Record<string, unknown> = {}): PublicRunEvent {
  return {
    schema_version: 'v1',
    run_id: 'run:1',
    sequence: seq,
    event_type: eventType,
    emitted_at: `2026-01-06T14:00:0${seq}Z`,
    stage: null,
    payload,
    artifact_refs: [],
  }
}

test('reconnect URL uses the last applied sequence as the cursor', () => {
  assert.equal(buildStreamUrl('run:1', 0), '/api/live-runs/run%3A1/stream')
  assert.equal(buildStreamUrl('run:1', 4), '/api/live-runs/run%3A1/stream?last_event_id=run%3A1:4')
})

test('parseSseFrame reconstructs the public event envelope', () => {
  const frame = [
    'id: run:1:3',
    'event: answer.delta',
    'data: {"schema_version":"v1","run_id":"run:1","sequence":3,"event_type":"answer.delta","emitted_at":"2026-01-06T14:00:03Z","payload":{"delta_text":"hi"},"artifact_refs":[]}',
    '',
  ].join('\n')
  const parsed = parseSseFrame(frame)
  assert.equal(parsed.sequence, 3)
  assert.equal(parsed.event_type, 'answer.delta')
  assert.equal(parsed.payload.delta_text, 'hi')
})

test('replay from cursor 0 equals live tail across a fake persisted producer', () => {
  const persisted: PublicRunEvent[] = [
    event(1, 'run.accepted'),
    event(2, 'stage.started', { stage: 'observation_build' }),
    event(3, 'answer.delta', { delta_text: 'A' }),
    event(4, 'answer.delta', { delta_text: 'B' }),
    event(5, 'run.completed', { result_status: 'SUFFICIENT' }),
  ]
  // Fake persisted-event producer: replay returns everything from cursor 0.
  function replayFrom(cursor: number): PublicRunEvent[] {
    return persisted.filter((e) => e.sequence > cursor)
  }
  // Browser reconnect from last applied sequence must yield the same final state.
  let replayed = createV1State()
  replayed = reduceEventBatch(replayed, replayFrom(0))
  let live = createV1State()
  for (const e of persisted) live = reduceEvent(live, e)
  assert.deepEqual(deriveDisplayState(replayed), deriveDisplayState(live))
  assert.equal(deriveDisplayState(replayed).answerText, 'AB')
})
