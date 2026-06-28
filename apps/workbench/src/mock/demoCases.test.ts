import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DEFAULT_DEMO_CASE_ID,
  DEMO_CASES,
  PIPELINE_STEP_IDS,
} from './demoCases.ts';

const causeDirections = new Set(['positive', 'negative', 'mixed', 'neutral']);
const causeRoles = new Set([
  'primary_driver',
  'secondary_driver',
  'supporting_context',
  'weak_context',
]);
const supportLevels = new Set(['strong', 'moderate', 'weak']);

test('ships coherent sufficient, partial, and insufficient demo cases', () => {
  assert.equal(DEFAULT_DEMO_CASE_ID, 'PARTIAL');
  assert.deepEqual(
    DEMO_CASES.map((demoCase) => demoCase.id),
    ['SUFFICIENT', 'PARTIAL', 'INSUFFICIENT'],
  );

  for (const demoCase of DEMO_CASES) {
    const evidenceIds = new Set(demoCase.candidateEvidence.map((item) => item.id));
    const citedIds = demoCase.attributionResult.causes.flatMap((cause) => cause.evidenceIds);

    assert.ok(demoCase.candles.length >= 12, `${demoCase.id} needs a usable price series`);
    const eventCandle = demoCase.candles[demoCase.candles.length - 1];
    const previousCandle = demoCase.candles[demoCase.candles.length - 2];
    const derivedMove = ((eventCandle.close - previousCandle.close) / previousCandle.close) * 100;
    assert.equal(eventCandle.date, demoCase.defaultSelectedDate, `${demoCase.id} must end on its selected date`);
    assert.ok(
      Math.abs(derivedMove - demoCase.movePct) < 0.15,
      `${demoCase.id} move must agree with its closing prices`,
    );
    assert.deepEqual(
      demoCase.pipelineCompleted.map((step) => step.id),
      PIPELINE_STEP_IDS,
      `${demoCase.id} must expose the complete pipeline`,
    );
    assert.ok(demoCase.pipelineBeforeRun.every((step) => step.status === 'pending'));
    assert.ok(demoCase.pipelineRunning.some((step) => step.status === 'active'));
    assert.ok(
      citedIds.every((id) => evidenceIds.has(id)),
      `${demoCase.id} cites evidence that is not present`,
    );
  }
});

test('partial and insufficient cases explain their evidence limits', () => {
  const partial = DEMO_CASES.find((demoCase) => demoCase.id === 'PARTIAL');
  const insufficient = DEMO_CASES.find((demoCase) => demoCase.id === 'INSUFFICIENT');

  assert.ok(partial?.attributionResult.statusReason.includes('missing'));
  assert.equal(partial?.attributionResult.refused, false);
  assert.equal(insufficient?.attributionResult.refused, true);
  assert.ok(insufficient?.attributionResult.statusReason.length);
  assert.equal(insufficient?.attributionResult.causes.length, 0);
});

test('attribution causes use explicit role, direction, and evidence support semantics', () => {
  const causes = DEMO_CASES.flatMap((demoCase) => demoCase.attributionResult.causes);

  assert.ok(causes.length > 0);
  for (const cause of causes) {
    assert.ok(causeDirections.has(cause.direction), `${cause.title} has an invalid direction`);
    assert.ok(causeRoles.has(cause.role), `${cause.title} needs an explicit role`);
    assert.ok(supportLevels.has(cause.supportLevel), `${cause.title} needs a support level`);
    const confidence = cause.confidence;
    assert.equal(typeof confidence, 'number', `${cause.title} needs mock evidence support`);
    if (confidence == null) throw new Error(`${cause.title} needs mock evidence support`);
    assert.ok(
      confidence >= 0 && confidence <= 1,
      `${cause.title} evidence support must be within 0-1`,
    );
    assert.equal('weight' in cause, false, `${cause.title} must not expose ambiguous cause weight`);
  }
});

test('primary drivers cite the strongest accepted evidence in attributed cases', () => {
  for (const demoCase of DEMO_CASES.filter((item) => !item.attributionResult.refused)) {
    const primary = demoCase.attributionResult.causes.find((cause) => cause.role === 'primary_driver');
    const strongestScore = Math.max(...demoCase.candidateEvidence.map((item) => item.relevanceScore));

    assert.ok(primary, `${demoCase.id} needs a primary driver`);
    assert.ok(
      primary.evidenceIds.some((id) => (
        demoCase.candidateEvidence.find((item) => item.id === id)?.relevanceScore === strongestScore
      )),
      `${demoCase.id} primary driver must cite its strongest evidence`,
    );
  }
});
