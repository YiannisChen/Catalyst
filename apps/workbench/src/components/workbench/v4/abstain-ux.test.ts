import { test } from 'node:test'
import assert from 'node:assert/strict'
import { abstainCopy, isAbstain, shouldShowAttributionBadge, NO_ACCEPTED_CAUSE_COPY } from './abstain-ux.ts'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))

test('ABSTAIN renders no accepted cause plus supplied limitations', () => {
  const copy = abstainCopy('ABSTAIN', ['News coverage only.'])
  assert.ok(copy)
  assert.ok(copy.includes(NO_ACCEPTED_CAUSE_COPY))
  assert.ok(copy.includes('News coverage only.'))
  assert.equal(isAbstain('ABSTAIN'), true)
})

test('attribution type badge is suppressed for ABSTAIN', () => {
  assert.equal(shouldShowAttributionBadge('ABSTAIN'), false)
  assert.equal(shouldShowAttributionBadge('EVIDENCE_BACKED_CAUSAL'), false)
})

test('attribution type badge is visible only for PARTIAL/SUFFICIENT', () => {
  assert.equal(shouldShowAttributionBadge('PARTIAL'), true)
  assert.equal(shouldShowAttributionBadge('SUFFICIENT'), true)
  assert.equal(shouldShowAttributionBadge('COMPLETED'), false)
  assert.equal(shouldShowAttributionBadge(null), false)
})

test('non-ABSTAIN statuses return no abstain copy', () => {
  assert.equal(abstainCopy('SUFFICIENT', []), null)
  assert.equal(abstainCopy('PARTIAL', ['gap']), null)
  assert.equal(abstainCopy(null, []), null)
})

test('live path has no demo fallback and no legacy semantic consumers', () => {
  const liveWorkbench = readFileSync(resolve(here, '../LiveWorkbench.tsx'), 'utf8')
  const workspaceAdapter = readFileSync(resolve(here, '../workspace-adapter.ts'), 'utf8')

  // No demo/mock fallback in the live path (no import from demo fixtures).
  assert.equal(liveWorkbench.includes('DemoWorkbench'), false)
  assert.equal(liveWorkbench.includes('mockRunData'), false)
  assert.equal(liveWorkbench.includes('terminal-fixture'), false)
  assert.equal(liveWorkbench.includes("from '../../mock/demoCases'"), false)

  // Live DTO consumers never read raw semantic internals.
  assert.equal(workspaceAdapter.includes('critic_reasoning'), false)
  assert.equal(workspaceAdapter.includes('judge_causes'), false)
  assert.equal(workspaceAdapter.includes('validator_decision'), false)
})
