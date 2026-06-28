/**
 * Scenario-specific mock artifacts for v4 workbench testing.
 * Keyed by DemoCaseId to prevent AAPL artifacts leaking into NVDA/TSLA.
 */
import type { ArtifactResponse } from '../api/types';

export const MOCK_RUN_ID = 'mock-run-001';

// ── NVDA (SUFFICIENT) artifacts ──
// Evidence IDs: ev-nvda-guidance, ev-nvda-filing, ev-chip-sector
// Trade date: 2025-06-10, Ticker: NVDA

const NVDA_ARTIFACTS: ArtifactResponse[] = [
  // Miner: retrieved_chunks
  {
    run_id: 'mock-nvda-001', event_seq: 1, node: 'miner',
    artifact_type: 'retrieved_chunks', created_at: '2025-06-10T14:00:02Z',
    payload: { chunks: [
      { rank: 1, asset_id: 'ev-nvda-guidance', ticker: 'NVDA', source_type: 'filing', reference_date: '2025-06-10', score: 0.94, headline: 'NVIDIA raises Q3 revenue guidance', snippet: 'NVIDIA announced updated fiscal Q3 guidance above consensus.' },
      { rank: 2, asset_id: 'ev-nvda-filing', ticker: 'NVDA', source_type: 'filing', reference_date: '2025-06-10', score: 0.91, headline: 'NVIDIA 8-K filing confirms raised guidance', snippet: 'The company filed an 8-K with the SEC confirming the guidance update.' },
      { rank: 3, asset_id: 'ev-chip-sector', ticker: 'NVDA', source_type: 'macro', reference_date: '2025-06-10', score: 0.68, headline: 'Semiconductor sector rallies on AI demand optimism', snippet: 'The Philadelphia Semiconductor Index rose 3.2%.' },
    ]},
  },
  // Miner: reranked_chunks
  {
    run_id: 'mock-nvda-001', event_seq: 1, node: 'miner',
    artifact_type: 'reranked_chunks', created_at: '2025-06-10T14:00:02Z',
    payload: { chunks: [
      { rank: 1, asset_id: 'ev-nvda-guidance', ticker: 'NVDA', source_type: 'filing', reference_date: '2025-06-10', score: 0.94, rerank_score: 0.96, headline: 'NVIDIA raises Q3 revenue guidance' },
      { rank: 2, asset_id: 'ev-nvda-filing', ticker: 'NVDA', source_type: 'filing', reference_date: '2025-06-10', score: 0.91, rerank_score: 0.93, headline: 'NVIDIA 8-K filing' },
      { rank: 3, asset_id: 'ev-chip-sector', ticker: 'NVDA', source_type: 'macro', reference_date: '2025-06-10', score: 0.68, rerank_score: 0.71, headline: 'Semiconductor sector rallies' },
    ]},
  },
  // Critic: graded_evidence
  {
    run_id: 'mock-nvda-001', event_seq: 2, node: 'critic',
    artifact_type: 'graded_evidence', created_at: '2025-06-10T14:00:06Z',
    payload: { items: [
      { chunk_id: 'ev-nvda-guidance', relevance: 'HIGH', category: 'earnings', temporal_match: true, reasoning: 'Direct guidance revision matching the trade date with specific revenue figures.', event_specificity: 'HIGH', temporal_alignment: 'EXACT', evidence_granularity: 'QUANTITATIVE', conflict_signal: false },
      { chunk_id: 'ev-nvda-filing', relevance: 'HIGH', category: 'regulatory', temporal_match: true, reasoning: 'SEC filing confirms the guidance update independently.', event_specificity: 'HIGH', temporal_alignment: 'EXACT', evidence_granularity: 'QUANTITATIVE', conflict_signal: false },
      { chunk_id: 'ev-chip-sector', relevance: 'MEDIUM', category: 'sector', temporal_match: true, reasoning: 'Sector read-through provides context but is not company-specific.', event_specificity: 'MEDIUM', temporal_alignment: 'NEAR', evidence_granularity: 'QUALITATIVE', conflict_signal: false },
    ]},
  },
  // Critic: critic_decision
  {
    run_id: 'mock-nvda-001', event_seq: 2, node: 'critic',
    artifact_type: 'critic_decision', created_at: '2025-06-10T14:00:06Z',
    payload: { decision: { verdict: 'SUFFICIENT', reasoning: 'Two independent primary sources directly support the guidance-driven move.', high_relevance_count: 2, medium_relevance_count: 1, low_relevance_count: 0 }},
  },
  // Judge: judge_causes — uses confidence, not weight
  {
    run_id: 'mock-nvda-001', event_seq: 3, node: 'judge',
    artifact_type: 'judge_causes', created_at: '2025-06-10T14:00:08Z',
    payload: { causes: [
      { text: 'Revenue guidance raised above consensus', category: 'earnings', direction: 'positive', confidence: 0.72, evidence_ids: ['ev-nvda-guidance', 'ev-nvda-filing'] },
      { text: 'Positive semiconductor read-through', category: 'sector', direction: 'positive', confidence: 0.28, evidence_ids: ['ev-chip-sector'] },
    ]},
  },
  // Judge: judge_summary
  {
    run_id: 'mock-nvda-001', event_seq: 3, node: 'judge',
    artifact_type: 'judge_summary', created_at: '2025-06-10T14:00:08Z',
    payload: { summary_md: '## NVDA +8.3% on 2025-06-10\n\nRevenue guidance raised above consensus drove the session, with supporting context from positive semiconductor sector read-through.', grounding_rate: 0.96 },
  },
  // Validator
  {
    run_id: 'mock-nvda-001', event_seq: 4, node: 'validator',
    artifact_type: 'validator_decision', created_at: '2025-06-10T14:00:12Z',
    payload: { output_status: 'SUFFICIENT', validation_error: null, validator_attempts: 1 },
  },
  // Raw LLM responses
  {
    run_id: 'mock-nvda-001', event_seq: 2, node: 'critic',
    artifact_type: 'raw_llm_response', created_at: '2025-06-10T14:00:06Z',
    payload: { text: '{"verdict":"SUFFICIENT","reasoning":"Two independent sources confirm guidance revision."}' },
  },
  {
    run_id: 'mock-nvda-001', event_seq: 3, node: 'judge',
    artifact_type: 'raw_llm_response', created_at: '2025-06-10T14:00:08Z',
    payload: { text: '{"causes":[{"text":"Revenue guidance raised","confidence":0.72}]}' },
  },
];

// ── AAPL (PARTIAL) artifacts ──
// Evidence IDs: ev-aapl-rates, ev-aapl-vision, ev-aapl-services, ev-aapl-iphone
// Trade date: 2025-09-08, Ticker: AAPL

const AAPL_ARTIFACTS: ArtifactResponse[] = [
  // Miner: retrieved_chunks
  {
    run_id: 'mock-aapl-001', event_seq: 1, node: 'miner',
    artifact_type: 'retrieved_chunks', created_at: '2025-09-08T10:00:02Z',
    payload: { chunks: [
      { rank: 1, asset_id: 'ev-aapl-rates', ticker: 'AAPL', source_type: 'macro', reference_date: '2025-09-08', score: 0.82, headline: 'US 10-year Treasury yield climbs to 4.35%', snippet: 'Treasury yields rose sharply pressuring long-duration technology shares.' },
      { rank: 2, asset_id: 'ev-aapl-vision', ticker: 'AAPL', source_type: 'news', reference_date: '2025-09-07', score: 0.61, headline: 'Analyst cuts Vision Pro shipment forecast', snippet: 'Shipment estimates reduced from 500K to 350K units.' },
      { rank: 3, asset_id: 'ev-aapl-services', ticker: 'AAPL', source_type: 'fundamentals', reference_date: '2025-09-06', score: 0.55, headline: 'Apple services revenue growth accelerates', snippet: 'App Store and subscription revenue growth reportedly accelerated.' },
      { rank: 4, asset_id: 'ev-aapl-iphone', ticker: 'AAPL', source_type: 'news', reference_date: '2025-09-07', score: 0.48, headline: 'iPhone 16 pre-order data shows mixed demand', snippet: 'Early tracking indicates stronger Pro model demand but weaker base model.' },
    ]},
  },
  // Miner: reranked_chunks
  {
    run_id: 'mock-aapl-001', event_seq: 1, node: 'miner',
    artifact_type: 'reranked_chunks', created_at: '2025-09-08T10:00:02Z',
    payload: { chunks: [
      { rank: 1, asset_id: 'ev-aapl-rates', ticker: 'AAPL', source_type: 'macro', reference_date: '2025-09-08', score: 0.82, rerank_score: 0.88, headline: 'US 10-year Treasury yield climbs' },
      { rank: 2, asset_id: 'ev-aapl-vision', ticker: 'AAPL', source_type: 'news', reference_date: '2025-09-07', score: 0.61, rerank_score: 0.65, headline: 'Analyst cuts Vision Pro forecast' },
      { rank: 3, asset_id: 'ev-aapl-services', ticker: 'AAPL', source_type: 'fundamentals', reference_date: '2025-09-06', score: 0.55, rerank_score: 0.58, headline: 'Apple services revenue growth' },
      { rank: 4, asset_id: 'ev-aapl-iphone', ticker: 'AAPL', source_type: 'news', reference_date: '2025-09-07', score: 0.48, rerank_score: 0.52, headline: 'iPhone 16 pre-order data' },
    ]},
  },
  // Critic: graded_evidence
  {
    run_id: 'mock-aapl-001', event_seq: 2, node: 'critic',
    artifact_type: 'graded_evidence', created_at: '2025-09-08T10:00:06Z',
    payload: { items: [
      { chunk_id: 'ev-aapl-rates', relevance: 'HIGH', category: 'macro_policy', temporal_match: true, reasoning: 'Rate movement directly impacts tech equity valuations.', event_specificity: 'HIGH', temporal_alignment: 'EXACT', evidence_granularity: 'QUALITATIVE', conflict_signal: false },
      { chunk_id: 'ev-aapl-vision', relevance: 'MEDIUM', category: 'product', temporal_match: false, reasoning: 'Relevant but unconfirmed and published before session.', event_specificity: 'MEDIUM', temporal_alignment: 'NEAR', evidence_granularity: 'QUANTITATIVE', conflict_signal: false },
      { chunk_id: 'ev-aapl-services', relevance: 'LOW', category: 'fundamentals', temporal_match: false, reasoning: 'Prior window fundamentals provide context but not a same-day catalyst.', event_specificity: 'LOW', temporal_alignment: 'PRIOR', evidence_granularity: 'QUALITATIVE', conflict_signal: false },
      { chunk_id: 'ev-aapl-iphone', relevance: 'LOW', category: 'product', temporal_match: false, reasoning: 'Mixed demand signal from prior window, insufficient as event driver.', event_specificity: 'LOW', temporal_alignment: 'PRIOR', evidence_granularity: 'QUALITATIVE', conflict_signal: false },
    ]},
  },
  // Critic: critic_decision
  {
    run_id: 'mock-aapl-001', event_seq: 2, node: 'critic',
    artifact_type: 'critic_decision', created_at: '2025-09-08T10:00:06Z',
    payload: { decision: { verdict: 'PARTIAL', reasoning: 'One strong macro driver supported. Product claim lacks independent confirmation.', high_relevance_count: 1, medium_relevance_count: 1, low_relevance_count: 2 }},
  },
  // Judge: judge_causes — confidence, not weight
  {
    run_id: 'mock-aapl-001', event_seq: 3, node: 'judge',
    artifact_type: 'judge_causes', created_at: '2025-09-08T10:00:08Z',
    payload: { causes: [
      { text: 'Higher yields pressured large-cap technology', category: 'macro', direction: 'negative', confidence: 0.58, evidence_ids: ['ev-aapl-rates'] },
      { text: 'Softer Vision Pro shipment expectations', category: 'product', direction: 'negative', confidence: 0.27, evidence_ids: ['ev-aapl-vision'] },
      { text: 'Services strength limited downside', category: 'fundamentals', direction: 'mixed', confidence: 0.15, evidence_ids: ['ev-aapl-services'] },
    ]},
  },
  // Judge: judge_summary
  {
    run_id: 'mock-aapl-001', event_seq: 3, node: 'judge',
    artifact_type: 'judge_summary', created_at: '2025-09-08T10:00:08Z',
    payload: { summary_md: '## AAPL -2.50% on 2025-09-08\n\nRate pressure is supported by same-day macro source. Product and services claims have moderate support.', grounding_rate: 0.78 },
  },
  // Validator
  {
    run_id: 'mock-aapl-001', event_seq: 4, node: 'validator',
    artifact_type: 'validator_decision', created_at: '2025-09-08T10:00:12Z',
    payload: { output_status: 'PARTIAL', validation_error: 'Missing independent confirmation for product claim.', validator_attempts: 1 },
  },
  // Raw LLM
  {
    run_id: 'mock-aapl-001', event_seq: 2, node: 'critic',
    artifact_type: 'raw_llm_response', created_at: '2025-09-08T10:00:06Z',
    payload: { text: '{"verdict":"PARTIAL","reasoning":"Rate driver confirmed. Product claim lacks corroboration."}' },
  },
  {
    run_id: 'mock-aapl-001', event_seq: 3, node: 'judge',
    artifact_type: 'raw_llm_response', created_at: '2025-09-08T10:00:08Z',
    payload: { text: '{"causes":[{"text":"Higher yields","confidence":0.58},{"text":"Vision Pro","confidence":0.27}]}' },
  },
];

// ── TSLA (INSUFFICIENT) artifacts ──
// No successful Judge. Critic returns insufficient.
// Trade date: 2025-09-22, Ticker: TSLA

const TSLA_ARTIFACTS: ArtifactResponse[] = [
  // Miner: retrieved_chunks
  {
    run_id: 'mock-tsla-001', event_seq: 1, node: 'miner',
    artifact_type: 'retrieved_chunks', created_at: '2025-09-22T10:00:02Z',
    payload: { chunks: [
      { rank: 1, asset_id: 'ev-tsla-sector', ticker: 'TSLA', source_type: 'news', reference_date: '2025-09-20', score: 0.35, headline: 'EV sector mixed as legacy automakers report', snippet: 'Read-through for Tesla remains uncertain.' },
      { rank: 2, asset_id: 'ev-tsla-reg', ticker: 'TSLA', source_type: 'news', reference_date: '2025-09-19', score: 0.42, headline: 'NHTSA opens preliminary investigation', snippet: 'Investigation into automated driving feature opened.' },
    ]},
  },
  // Critic: graded_evidence
  {
    run_id: 'mock-tsla-001', event_seq: 2, node: 'critic',
    artifact_type: 'graded_evidence', created_at: '2025-09-22T10:00:04Z',
    payload: { items: [
      { chunk_id: 'ev-tsla-sector', relevance: 'LOW', category: 'sector', temporal_match: false, reasoning: 'Generic sector commentary, not specific to Tesla event.', event_specificity: 'LOW', temporal_alignment: 'PRIOR', evidence_granularity: 'QUALITATIVE', conflict_signal: false },
      { chunk_id: 'ev-tsla-reg', relevance: 'LOW', category: 'regulatory', temporal_match: false, reasoning: 'Preliminary investigation from prior window, unrelated to session move.', event_specificity: 'LOW', temporal_alignment: 'PRIOR', evidence_granularity: 'QUALITATIVE', conflict_signal: false },
    ]},
  },
  // Critic: critic_decision
  {
    run_id: 'mock-tsla-001', event_seq: 2, node: 'critic',
    artifact_type: 'critic_decision', created_at: '2025-09-22T10:00:04Z',
    payload: { decision: { verdict: 'INSUFFICIENT', reasoning: 'No timely, company-specific evidence passed relevance checks.', high_relevance_count: 0, medium_relevance_count: 0, low_relevance_count: 2 }},
  },
  // Critic raw LLM
  {
    run_id: 'mock-tsla-001', event_seq: 2, node: 'critic',
    artifact_type: 'raw_llm_response', created_at: '2025-09-22T10:00:04Z',
    payload: { text: '{"verdict":"INSUFFICIENT","reasoning":"No evidence meets relevance threshold for attribution."}' },
  },
  // Validator (refusal)
  {
    run_id: 'mock-tsla-001', event_seq: 3, node: 'validator',
    artifact_type: 'validator_decision', created_at: '2025-09-22T10:00:06Z',
    payload: { output_status: 'INSUFFICIENT', validation_error: null, validator_attempts: 1 },
  },
];

// ── Public exports ──
import type { DemoCaseId } from '../mock/demoCases';
import type { RunEventResponse } from '../api/types';

export const MOCK_ARTIFACTS_BY_CASE: Record<DemoCaseId, ArtifactResponse[]> = {
  SUFFICIENT: NVDA_ARTIFACTS,
  PARTIAL: AAPL_ARTIFACTS,
  INSUFFICIENT: TSLA_ARTIFACTS,
};

export const MOCK_RUNTIME_MS_BY_CASE: Record<DemoCaseId, number> = {
  SUFFICIENT: 28400,
  PARTIAL: 31200,
  INSUFFICIENT: 18500,
};

// Legacy exports for existing LiveWorkbench (Ctrl+Shift+M) — kept backward compatible
export const MOCK_ARTIFACTS: ArtifactResponse[] = AAPL_ARTIFACTS;

export const MOCK_SUMMARY_RUNNING = {
  run_id: MOCK_RUN_ID, status: 'RUNNING' as const,
  ticker: 'AAPL', trade_date: '2025-09-08', model_id: 'gemini-2.5-flash-nothink',
  last_completed_node: 'miner', predicted_next_node: 'critic',
};

export const MOCK_SUMMARY_SUCCEEDED = {
  run_id: MOCK_RUN_ID, status: 'SUCCEEDED' as const,
  ticker: 'AAPL', trade_date: '2025-09-08', model_id: 'gemini-2.5-flash-nothink',
  last_completed_node: 'validator', predicted_next_node: null,
};

export const MOCK_SUMMARY_FAILED = {
  run_id: MOCK_RUN_ID, status: 'FAILED_SYSTEM' as const,
  ticker: 'AAPL', trade_date: '2025-09-08', model_id: 'gemini-2.5-flash-nothink',
  last_completed_node: 'critic', predicted_next_node: null,
  failure: { status: 'FAILED_SYSTEM', sub_reason: 'timeout', message: 'LLM call timed out after 120s.', source: 'critic', node: 'critic', field: null, retryable: true },
  retry: { parent_run_id: null, retry_count: 0, retryable: true },
};

export const MOCK_EVENTS: RunEventResponse[] = [
  { run_id: MOCK_RUN_ID, trace_id: 'trace-001', event_seq: 1, node: 'miner', status_before: 'QUEUED', status_after: 'RUNNING', model_id: null, started_at: '2025-09-08T10:00:00Z', ended_at: '2025-09-08T10:00:02.340Z', latency_ms: 2340, input_tokens: null, output_tokens: null, cost_usd: null },
  { run_id: MOCK_RUN_ID, trace_id: 'trace-001', event_seq: 3, node: 'critic', status_before: 'RUNNING', status_after: 'RUNNING', model_id: 'gemini-2.5-flash-nothink', started_at: '2025-09-08T10:00:04.780Z', ended_at: '2025-09-08T10:00:12.150Z', latency_ms: 7370, input_tokens: 3842, output_tokens: 512, cost_usd: 0.0023 },
  { run_id: MOCK_RUN_ID, trace_id: 'trace-001', event_seq: 4, node: 'judge', status_before: 'RUNNING', status_after: 'RUNNING', model_id: 'gemini-2.5-flash-nothink', started_at: '2025-09-08T10:00:12.150Z', ended_at: '2025-09-08T10:00:24.800Z', latency_ms: 12650, input_tokens: 5120, output_tokens: 1024, cost_usd: 0.0051 },
  { run_id: MOCK_RUN_ID, trace_id: 'trace-001', event_seq: 5, node: 'validator', status_before: 'RUNNING', status_after: 'SUCCEEDED', model_id: 'gemini-2.5-flash-nothink', started_at: '2025-09-08T10:00:24.800Z', ended_at: '2025-09-08T10:00:31.200Z', latency_ms: 6400, input_tokens: 2048, output_tokens: 256, cost_usd: 0.0012 },
];
