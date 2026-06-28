import assert from 'node:assert/strict';
import test from 'node:test';

import { getDemoCase } from '../../mock/demoCases.ts';
import {
  collectReferencedEvidenceIds,
  getCandidateEvidence,
  getSelectedDayMetrics,
  resolveAttributionResult,
} from './workbench-model.ts';

test('collects unique evidence references from attribution causes', () => {
  const partial = getDemoCase('PARTIAL');

  assert.deepEqual(
    [...collectReferencedEvidenceIds(partial.attributionResult)],
    ['ev-aapl-rates', 'ev-aapl-vision', 'ev-aapl-services'],
  );
});

test('derives metrics from the selected candle instead of the scenario result', () => {
  const partial = getDemoCase('PARTIAL');
  const metrics = getSelectedDayMetrics(partial, partial.defaultSelectedDate);

  assert.equal(metrics?.date, '2025-09-08');
  assert.equal(metrics?.closeMove.toFixed(1), '-2.5');
  assert.equal(metrics?.volume, 89_000_000);
  assert.ok((metrics?.intradayRange ?? 0) > 0);
  assert.equal(metrics?.eventWindow, 'Sep 4-8, 2025');
});

test('clears scenario citations when a different candle is analyzed', () => {
  const partial = getDemoCase('PARTIAL');
  const otherDate = partial.candles[4].date;

  assert.deepEqual(getCandidateEvidence(partial, otherDate), []);
  const result = resolveAttributionResult(partial, otherDate);
  assert.equal(result.status, 'INSUFFICIENT');
  assert.equal(result.refused, true);
  assert.equal(result.causes.length, 0);
});
