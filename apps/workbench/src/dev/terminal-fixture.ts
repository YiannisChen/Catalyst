/**
 * Terminal workspace fixture for visual regression QA.
 *
 * Imported ONLY behind VITE_ENABLE_DEMO=1 gate or in test files.
 * NEVER imported in LiveWorkbench.tsx production path.
 */

import type { WorkspaceResponse } from '../api/types';
import type { AttributionResult, EvidenceItem, PipelineStep } from '../mock/demoCases';

export interface TerminalFixture {
  workspace: WorkspaceResponse;
  evidence: EvidenceItem[];
  result: AttributionResult;
  steps: PipelineStep[];
}

export const TERMINAL_FIXTURE: TerminalFixture = {
  workspace: {
    run_id: 'fixture-terminal-001',
    status: 'SUCCEEDED',
    runtime_ms: 2847,
    evidence: [
      {
        id: 'ev-fixture-001',
        headline: 'Fed signals rate pause amid cooling inflation data',
        snippet:
          'The Federal Reserve indicated it may pause interest rate hikes after new data showed inflation cooling to 2.4% in May, below analyst expectations of 2.6%. The decision boosted market sentiment across sectors, with technology stocks leading gains.',
        source_type: 'news',
        reference_date: '2025-09-02',
        critic_decision: 'accepted',
        cited: true,
      },
      {
        id: 'ev-fixture-002',
        headline: 'iPhone 16 pre-orders exceed analyst estimates by 15%',
        snippet:
          'Apple Inc. iPhone 16 pre-order volumes reached 12-15% above consensus estimates in the first 48 hours, according to supply chain checks and carrier data reviewed by Bloomberg. The stronger-than-expected demand suggests a robust upgrade cycle.',
        source_type: 'news',
        reference_date: '2025-09-02',
        critic_decision: 'accepted',
        cited: true,
      },
      {
        id: 'ev-fixture-003',
        headline: 'Tech sector rallies as Treasury yields decline',
        snippet:
          'Technology stocks rallied broadly on Monday as the 10-year Treasury yield fell below 3.8%, easing pressure on growth stock valuations. The Nasdaq Composite gained 1.8%, with mega-cap names leading the advance.',
        source_type: 'news',
        reference_date: '2025-09-02',
        critic_decision: 'accepted',
        cited: false,
      },
      {
        id: 'ev-fixture-004',
        headline: 'Apple supplier Foxconn reports strong August revenue',
        snippet:
          'Foxconn Technology Group, Apple\'s largest manufacturing partner, reported August revenue of NT$548.3 billion, up 12% year-over-year, driven by strong smartphone component orders ahead of the new iPhone launch cycle.',
        source_type: 'news',
        reference_date: '2025-09-01',
        critic_decision: 'accepted',
        cited: false,
      },
      {
        id: 'ev-fixture-005',
        headline: 'Apple faces EU regulatory probe over App Store policies',
        snippet:
          'The European Commission announced a formal investigation into Apple\'s App Store policies under the Digital Markets Act, potentially exposing the company to fines of up to 10% of global annual revenue.',
        source_type: 'news',
        reference_date: '2025-09-02',
        critic_decision: 'rejected',
        cited: false,
      },
    ],
    stages: [
      {
        id: 'retrieval',
        label: 'Retrieval',
        status: 'complete',
        duration_ms: 820,
        artifact_types: [],
      },
      {
        id: 'critic',
        label: 'Critic',
        status: 'complete',
        duration_ms: 640,
        artifact_types: [],
      },
      {
        id: 'judge',
        label: 'Judge',
        status: 'complete',
        duration_ms: 980,
        artifact_types: [],
      },
      {
        id: 'finalized',
        label: 'Finalized',
        status: 'complete',
        duration_ms: 407,
        artifact_types: [],
      },
    ],
    result: {
      output_status: 'sufficient',
      summary_md:
        'The price movement was primarily attributed to macroeconomic tailwinds from the Federal Reserve\'s rate pause signal, combined with better-than-expected iPhone 16 pre-order figures reported by multiple analysts. Supply chain checks confirmed sustained demand in key markets. The EU regulatory probe was considered but dismissed as insufficiently impactful for the single-day price action.',
      grounding_rate: 0.78,
      causes: [
        {
          text: 'Fed rate pause signal boosts tech sector',
          category: 'macro',
          direction: 'positive',
          confidence: 0.85,
          evidence_ids: ['ev-fixture-001', 'ev-fixture-003'],
        },
        {
          text: 'iPhone 16 pre-orders exceed analyst estimates',
          category: 'company',
          direction: 'positive',
          confidence: 0.72,
          evidence_ids: ['ev-fixture-002', 'ev-fixture-004'],
        },
      ],
    },
  },

  // Mapped shapes matching workspace-adapter output
  evidence: [
    {
      id: 'ev-fixture-001',
      title: 'Fed signals rate pause amid cooling inflation data',
      snippet:
        'The Federal Reserve indicated it may pause interest rate hikes after new data showed inflation cooling to 2.4% in May, below analyst expectations of 2.6%. The decision boosted market sentiment across sectors, with technology stocks leading gains.',
      source: 'Reuters',
      sourceType: 'news',
      publishedAt: '2025-09-02T14:30:00Z',
      quality: 'high',
      temporalStatus: 'same-day',
      relevanceScore: 0.92,
      criticDecision: 'accepted',
    },
    {
      id: 'ev-fixture-002',
      title: 'iPhone 16 pre-orders exceed analyst estimates by 15%',
      snippet:
        'Apple Inc. iPhone 16 pre-order volumes reached 12-15% above consensus estimates in the first 48 hours, according to supply chain checks and carrier data reviewed by Bloomberg. The stronger-than-expected demand suggests a robust upgrade cycle.',
      source: 'Bloomberg',
      sourceType: 'news',
      publishedAt: '2025-09-02T16:15:00Z',
      quality: 'high',
      temporalStatus: 'same-day',
      relevanceScore: 0.88,
      criticDecision: 'accepted',
    },
    {
      id: 'ev-fixture-003',
      title: 'Tech sector rallies as Treasury yields decline',
      snippet:
        'Technology stocks rallied broadly on Monday as the 10-year Treasury yield fell below 3.8%, easing pressure on growth stock valuations. The Nasdaq Composite gained 1.8%, with mega-cap names leading the advance.',
      source: 'CNBC',
      sourceType: 'news',
      publishedAt: '2025-09-02T15:45:00Z',
      quality: 'medium',
      temporalStatus: 'same-day',
      relevanceScore: 0.85,
      criticDecision: 'accepted',
    },
    {
      id: 'ev-fixture-004',
      title: 'Apple supplier Foxconn reports strong August revenue',
      snippet:
        'Foxconn Technology Group, Apple\'s largest manufacturing partner, reported August revenue of NT$548.3 billion, up 12% year-over-year, driven by strong smartphone component orders ahead of the new iPhone launch cycle.',
      source: 'WSJ',
      sourceType: 'news',
      publishedAt: '2025-09-01T08:00:00Z',
      quality: 'medium',
      temporalStatus: 'prior-window',
      relevanceScore: 0.81,
      criticDecision: 'accepted',
    },
    {
      id: 'ev-fixture-005',
      title: 'Apple faces EU regulatory probe over App Store policies',
      snippet:
        'The European Commission announced a formal investigation into Apple\'s App Store policies under the Digital Markets Act, potentially exposing the company to fines of up to 10% of global annual revenue.',
      source: 'MarketWatch',
      sourceType: 'news',
      publishedAt: '2025-09-02T10:00:00Z',
      quality: 'low',
      temporalStatus: 'same-day',
      relevanceScore: 0.35,
      criticDecision: 'rejected',
    },
  ],

  result: {
    status: 'AAPL' as AttributionResult['status'],
    label: 'AAPL +2.31% on 2025-09-02',
    headline:
      'Apple shares rose 2.31% driven by positive Fed policy signal and strong iPhone 16 pre-order data.',
    summary:
      'The price movement was primarily attributed to macroeconomic tailwinds from the Federal Reserve\'s rate pause signal, combined with better-than-expected iPhone 16 pre-order figures reported by multiple analysts. Supply chain checks confirmed sustained demand in key markets. The EU regulatory probe was considered but dismissed as insufficiently impactful for the single-day price action.',
    statusReason:
      'Evidence boundary: sufficient — 4 items accepted from 5 candidates. Grounding rate 78%. Two primary drivers identified with high confidence.',
    groundingRate: 0.78,
    refused: false,
    causes: [
      {
        title: 'Fed rate pause signal boosts tech sector',
        direction: 'positive',
        role: 'primary_driver',
        supportLevel: 'strong',
        confidence: 0.85,
        rationale:
          'The Federal Reserve\'s indication of a potential rate pause created broad-based buying pressure in technology stocks, with Apple as a primary beneficiary due to its market capitalization weighting in major indices.',
        evidenceIds: ['ev-fixture-001', 'ev-fixture-003'],
      },
      {
        title: 'iPhone 16 pre-orders exceed analyst estimates',
        direction: 'positive',
        role: 'secondary_driver',
        supportLevel: 'moderate',
        confidence: 0.72,
        rationale:
          'Multiple supply chain reports and carrier data indicated iPhone 16 pre-order volumes were 12-15% above consensus estimates, suggesting stronger than expected demand for the new model cycle. Foxconn supplier data corroborated this trend.',
        evidenceIds: ['ev-fixture-002', 'ev-fixture-004'],
      },
    ],
  },

  steps: [
    {
      id: 'retrieval',
      label: 'Retrieval',
      status: 'complete',
      note: 'Retrieved 12 candidate chunks from News API, filtered to 5 relevant items.',
      durationMs: 820,
    },
    {
      id: 'critic',
      label: 'Critic',
      status: 'complete',
      note: 'Evaluated 5 items: 4 accepted, 1 rejected (regulatory probe — insufficient market impact).',
      durationMs: 640,
    },
    {
      id: 'judge',
      label: 'Judge',
      status: 'complete',
      note: 'Synthesized causes from 4 accepted evidence items.',
      durationMs: 980,
    },
    {
      id: 'finalized',
      label: 'Finalized',
      status: 'complete',
      note: 'Finalized attribution with 2 primary drivers, grounding rate 78%.',
      durationMs: 407,
    },
  ],
};

/**
 * Validate fixture shape at import time.
 * Throws if any required field is missing or empty.
 */
export function validateFixture(f: TerminalFixture): true {
  const { workspace, evidence, result, steps } = f;

  if (!workspace.run_id) throw new Error('fixture: missing workspace.run_id');
  if (!workspace.status) throw new Error('fixture: missing workspace.status');
  if (workspace.runtime_ms == null) throw new Error('fixture: missing workspace.runtime_ms');

  if (evidence.length < 5) throw new Error('fixture: need >= 5 evidence items');
  const cited = evidence.filter((e) => e.criticDecision === 'accepted');
  if (cited.length < 2) throw new Error('fixture: need >= 2 accepted evidence items');
  const rejected = evidence.filter((e) => e.criticDecision === 'rejected');
  if (rejected.length < 1) throw new Error('fixture: need >= 1 rejected evidence item');

  if (!result.headline) throw new Error('fixture: missing result.headline');
  if (result.summary.length < 200) throw new Error('fixture: summary must be >= 200 chars');
  if (result.causes.length < 2) throw new Error('fixture: need >= 2 causes');

  if (steps.length < 4) throw new Error('fixture: need >= 4 pipeline steps');

  return true;
}
