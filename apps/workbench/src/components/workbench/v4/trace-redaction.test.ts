/**
 * Tests for trace-redaction.ts and fixture invariants.
 */
import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import { normalizeSensitiveKey, redactPayload } from './trace-redaction.ts';
import {
  DEMO_CASES,
  type AttributionCause,
  type EvidenceItem,
} from '../../../mock/demoCases.ts';

const traceSource = readFileSync(new URL('./TraceTab.tsx', import.meta.url), 'utf8');
const mockRunData = readFileSync(new URL('../../../dev/mockRunData.ts', import.meta.url), 'utf8');

/* ── 1. Key normalisation ── */

test('normalizes keys correctly across formats', () => {
  assert.equal(normalizeSensitiveKey('api_key'), 'apikey');
  assert.equal(normalizeSensitiveKey('api-key'), 'apikey');
  assert.equal(normalizeSensitiveKey('API_KEY'), 'apikey');
  assert.equal(normalizeSensitiveKey('ApiKey'), 'apikey');
  assert.equal(normalizeSensitiveKey('access_token'), 'accesstoken');
  assert.equal(normalizeSensitiveKey('access-token'), 'accesstoken');
  assert.equal(normalizeSensitiveKey('client_secret'), 'clientsecret');
  assert.equal(normalizeSensitiveKey('private_key'), 'privatekey');
});

/* ── 2. Object redaction ── */

test('redacts nested sensitive fields', () => {
  const input = {
    api_key: 'secret-1',
    nested: {
      accessToken: 'secret-2',
      authorization: 'Bearer secret-3',
    },
    safe: 'keep-me',
  };
  const original = JSON.parse(JSON.stringify(input));
  const result = redactPayload(input) as Record<string, unknown>;

  assert.equal(result.api_key, '[REDACTED]');
  assert.equal((result.nested as Record<string, unknown>).accessToken, '[REDACTED]');
  assert.equal((result.nested as Record<string, unknown>).authorization, '[REDACTED]');
  assert.equal(result.safe, 'keep-me');
  assert.deepEqual(input, original, 'source object must be unchanged');
});

test('redacts in arrays', () => {
  const input = [{ secret: 'x' }, { password: 'y' }, { ok: 'z' }];
  const result = redactPayload(input) as Array<Record<string, unknown>>;
  assert.equal(result[0].secret, '[REDACTED]');
  assert.equal(result[1].password, '[REDACTED]');
  assert.equal(result[2].ok, 'z');
});

/* ── 3. Plain-text Bearer redaction ── */

test('redacts plain-text Bearer tokens', () => {
  assert.equal(redactPayload('Bearer abc'), '[REDACTED]');
  assert.equal(redactPayload('bearer xyz'), '[REDACTED]');
  assert.equal(redactPayload('Not a bearer'), 'Not a bearer');
});

test('redacts Bearer tokens inline within text', () => {
  assert.equal(
    redactPayload('authorization: Bearer abc123 def'),
    'authorization: [REDACTED] def',
  );
  assert.equal(
    redactPayload('foo Bearer xyz bar'),
    'foo [REDACTED] bar',
  );
  assert.equal(
    redactPayload('multiple bearer abc and bearer def tokens'),
    'multiple [REDACTED] and [REDACTED] tokens',
  );
});

/* ── 4. JSON-string redaction ── */

test('redacts api_key inside a JSON string payload', () => {
  const input = '{"api_key":"sk-12345","query":"test"}';
  const result = redactPayload(input);
  assert.deepEqual(result, { api_key: '[REDACTED]', query: 'test' });
});

test('redacts nested tokens inside a JSON string payload', () => {
  const input = '{"prompt": "hello", "system_prompt": "you are a bot", "nested": {"token": "abc"}}';
  const result = redactPayload(input) as Record<string, unknown>;
  assert.equal(result.system_prompt, '[REDACTED]');
  assert.equal((result.nested as Record<string, unknown>).token, '[REDACTED]');
  assert.equal(result.prompt, 'hello');
});

test('redacts authorization in JSON string payload', () => {
  const input = '{"authorization":"Bearer sk-abc","data":"ok"}';
  const result = redactPayload(input);
  assert.deepEqual(result, { authorization: '[REDACTED]', data: 'ok' });
});

test('returns string unchanged when JSON parse fails', () => {
  const input = 'not valid json at all';
  const result = redactPayload(input);
  assert.equal(result, 'not valid json at all');
});

test('redacts Bearer token in non-JSON string', () => {
  const input = 'Error: authorization failed with Bearer abc123xyz';
  const result = redactPayload(input);
  assert.equal(result, 'Error: authorization failed with [REDACTED]');
});

test('handles JSON array payload', () => {
  const input = '[{"api_key":"a"},{"secret":"b"}]';
  const result = redactPayload(input);
  assert.deepEqual(result, [
    { api_key: '[REDACTED]' },
    { secret: '[REDACTED]' },
  ]);
});

/* ── 5. Source immutability ── */

test('redactPayload does not mutate its input', () => {
  const input = { api_key: 'keep-me-original', items: [{ token: 't' }] };
  const frozen = JSON.parse(JSON.stringify(input));
  redactPayload(input);
  assert.deepEqual(input, frozen);
});

/* ── 6. Cited ⊆ accepted — data-driven ── */

test('every cited evidence item is accepted in all demo cases', () => {
  for (const dc of DEMO_CASES) {
    const evidenceById = new Map<string, EvidenceItem>(
      dc.candidateEvidence.map((e) => [e.id, e]),
    );

    const allCitedIds = dc.attributionResult.causes.flatMap(
      (c: AttributionCause) => c.evidenceIds,
    );

    for (const eid of allCitedIds) {
      const ev = evidenceById.get(eid);
      assert.ok(ev, `${dc.id}: cited evidence "${eid}" must exist in candidateEvidence`);
      assert.equal(
        ev.criticDecision,
        'accepted',
        `${dc.id}: cited evidence "${eid}" must be accepted, got "${ev.criticDecision}"`,
      );
    }
  }
});

/* ── 7. Artifact isolation ── */

test('mockRunData has scenario-specific artifacts', () => {
  assert.match(mockRunData, /MOCK_ARTIFACTS_BY_CASE/);
  assert.match(mockRunData, /MOCK_RUNTIME_MS_BY_CASE/);
});

test('NVDA artifacts contain no AAPL data', () => {
  const nvda = mockRunData.split('// ── AAPL')[0] ?? '';
  assert.doesNotMatch(nvda, /ev-aapl/);
  assert.doesNotMatch(nvda, /Apple/);
});

test('TSLA artifacts contain no AAPL or NVDA IDs', () => {
  const tsla =
    (mockRunData.split('// ── Public exports')[0] ?? '').split('// ── TSLA')[1] ?? '';
  assert.doesNotMatch(tsla, /ev-aapl/);
  assert.doesNotMatch(tsla, /ev-nvda/);
});

test('TSLA has no judge_causes artifact', () => {
  const tsla =
    (mockRunData.split('// ── Public exports')[0] ?? '').split('// ── TSLA')[1] ?? '';
  assert.doesNotMatch(tsla, /judge_causes/);
  assert.doesNotMatch(tsla, /judge_summary/);
});

/* ── 8. Trace source contracts ── */

test('TraceTab imports redaction from trace-redaction module', () => {
  assert.match(
    traceSource,
    /from '\.\/trace-redaction'/,
  );
});

test('TraceTab has raw output toggle with accessibility roles', () => {
  assert.match(traceSource, /role="tablist"/);
  assert.match(traceSource, /role="tab"/);
});

test('TraceTab has payload size limits', () => {
  assert.match(traceSource, /RAW_RENDER_LIMIT/);
  assert.match(traceSource, /RAW_COPY_LIMIT/);
  assert.match(traceSource, /Output truncated/);
});
