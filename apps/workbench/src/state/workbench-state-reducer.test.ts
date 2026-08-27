import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  createV1State,
  reduceEvent,
  reduceEventBatch,
  reduceRunDto,
  deriveDisplayState,
} from './workbench-state.ts'
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

const accepted = event(1, 'run.accepted', { stream_url: '/s' })
const stage = event(2, 'stage.started', { stage: 'observation_build' })
const delta1 = event(3, 'answer.delta', { delta_text: 'Strong ', delta_ordinal: 1, cumulative_character_count: 7 })
const delta2 = event(4, 'answer.delta', { delta_text: 'revenue.', delta_ordinal: 2, cumulative_character_count: 15 })
const completed = event(5, 'run.completed', { result_status: 'SUFFICIENT' })

test('applies events in sequence order', () => {
  let state = createV1State()
  state = reduceEvent(state, accepted)
  state = reduceEvent(state, stage)
  state = reduceEvent(state, delta1)
  assert.equal(state.lastAppliedSequence, 3)
  assert.equal(state.lifecycleStatus, 'ACCEPTED')
})

test('deduplicates by (run_id, sequence)', () => {
  let state = createV1State()
  state = reduceEvent(state, accepted)
  state = reduceEvent(state, accepted)
  state = reduceEvent(state, accepted)
  assert.equal(state.lastAppliedSequence, 1)
  assert.equal(state.appliedKeys.size, 1)
})

test('detects sequence gaps', () => {
  let state = createV1State()
  state = reduceEvent(state, accepted)
  state = reduceEvent(state, event(3, 'answer.delta', { delta_text: 'x' }))
  assert.equal(state.gapDetected, true)
})

test('replay from cursor 0 equals live tail display state', () => {
  const persisted = [accepted, stage, delta1, delta2, completed]
  // Replay path: whole batch.
  let replay = createV1State()
  replay = reduceEventBatch(replay, persisted)
  // Live path: same events delivered incrementally.
  let live = createV1State()
  for (const e of persisted) live = reduceEvent(live, e)
  const replayDisplay = deriveDisplayState(replay)
  const liveDisplay = deriveDisplayState(live)
  assert.deepEqual(replayDisplay, liveDisplay)
  assert.equal(liveDisplay.answerText, 'Strong revenue.')
  assert.equal(liveDisplay.lifecycleStatus, 'COMPLETED')
})

test('provisional output invalidated on assurance failure', () => {
  let state = createV1State()
  state = reduceEvent(state, accepted)
  state = reduceEvent(state, delta1)
  state = reduceEvent(state, event(5, 'assurance.completed', { valid: false, provisional_invalidated: true }))
  const display = deriveDisplayState(state)
  assert.equal(display.provisionalInvalidated, true)
})

test('reducer never fabricates attribution semantics from raw values', () => {
  let state = createV1State()
  // Even a completed event with a result_status must NOT set attribution status.
  state = reduceEvent(state, accepted)
  state = reduceEvent(state, completed)
  const display = deriveDisplayState(state)
  assert.equal(display.attributionStatus, null)
  assert.equal(display.attributionType, null)
  assert.equal('primaryCause' in display, false)
  assert.equal('supportStrength' in display, false)
  assert.equal('materiality' in display, false)
  assert.equal('independence' in display, false)
  assert.equal('claimLinks' in display, false)
  assert.equal('assurance' in display, false)
})

test('attribution comes only from the authoritative RunDTO', () => {
  let state = createV1State()
  state = reduceEvent(state, accepted)
  state = reduceRunDto(state, {
    run_id: 'run:1',
    lifecycle_status: 'COMPLETED',
    attribution_status: 'PARTIAL',
    attribution_type: 'EVIDENCE_BACKED_CAUSAL',
    created_at: '2026-01-06T14:00:00Z',
    manifest_summary: {},
    terminal_artifact_refs: [],
  })
  const display = deriveDisplayState(state)
  assert.equal(display.attributionStatus, 'PARTIAL')
  assert.equal(display.attributionBadgeVisible, true)
})

test('ABSTAIN renders no accepted cause and suppresses type badge', () => {
  let state = createV1State()
  state = reduceEvent(state, accepted)
  state = reduceRunDto(state, {
    run_id: 'run:1',
    lifecycle_status: 'COMPLETED',
    attribution_status: 'ABSTAIN',
    attribution_type: 'EVIDENCE_BACKED_CAUSAL',
    created_at: '2026-01-06T14:00:00Z',
    manifest_summary: {},
    terminal_artifact_refs: [],
    limitations: ['News coverage only.'],
  })
  const display = deriveDisplayState(state)
  assert.equal(display.noAcceptedCause, true)
  assert.equal(display.attributionBadgeVisible, false)
  assert.equal(display.limitations.length, 1)
})
