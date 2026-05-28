/**
 * Mock data for testing the attribution runtime UI without a real backend.
 * Press Ctrl+Shift+M in dev mode to inject mock data.
 *
 * Payload shapes match catalyst_agents.trace.projection output.
 */
import type { RunEventResponse, ArtifactResponse } from '../api/types';

export const MOCK_RUN_ID = 'mock-run-001';

export const MOCK_SUMMARY_RUNNING = {
  run_id: MOCK_RUN_ID,
  status: 'RUNNING' as const,
  ticker: 'AAPL',
  trade_date: '2025-09-08',
  model_id: 'gemini-2.5-flash-nothink',
  last_completed_node: 'miner',
  predicted_next_node: 'critic',
};

export const MOCK_SUMMARY_SUCCEEDED = {
  run_id: MOCK_RUN_ID,
  status: 'SUCCEEDED' as const,
  ticker: 'AAPL',
  trade_date: '2025-09-08',
  model_id: 'gemini-2.5-flash-nothink',
  last_completed_node: 'validator',
  predicted_next_node: null,
};

export const MOCK_SUMMARY_FAILED = {
  run_id: MOCK_RUN_ID,
  status: 'FAILED_SYSTEM' as const,
  ticker: 'AAPL',
  trade_date: '2025-09-08',
  model_id: 'gemini-2.5-flash-nothink',
  last_completed_node: 'critic',
  predicted_next_node: null,
  failure: {
    status: 'FAILED_SYSTEM' as const,
    sub_reason: 'timeout',
    message: 'LLM call timed out after 120s while processing critic node. The model did not respond within the configured timeout window.',
    source: 'critic',
    node: 'critic',
    field: null,
    retryable: true,
  },
  retry: {
    parent_run_id: null,
    retry_count: 0,
    retryable: true,
  },
};

export const MOCK_EVENTS: RunEventResponse[] = [
  {
    run_id: MOCK_RUN_ID,
    trace_id: 'trace-001',
    event_seq: 1,
    node: 'miner',
    status_before: 'QUEUED',
    status_after: 'RUNNING',
    model_id: null,
    started_at: '2025-09-08T10:00:00Z',
    ended_at: '2025-09-08T10:00:02.340Z',
    latency_ms: 2340,
    input_tokens: null,
    output_tokens: null,
    cost_usd: null,
  },
  {
    run_id: MOCK_RUN_ID,
    trace_id: 'trace-001',
    event_seq: 2,
    node: 'miner',
    status_before: 'RUNNING',
    status_after: 'RUNNING',
    model_id: null,
    started_at: '2025-09-08T10:00:02.340Z',
    ended_at: '2025-09-08T10:00:04.780Z',
    latency_ms: 2440,
    input_tokens: null,
    output_tokens: null,
    cost_usd: null,
  },
  {
    run_id: MOCK_RUN_ID,
    trace_id: 'trace-001',
    event_seq: 3,
    node: 'critic',
    status_before: 'RUNNING',
    status_after: 'RUNNING',
    model_id: 'gemini-2.5-flash-nothink',
    started_at: '2025-09-08T10:00:04.780Z',
    ended_at: '2025-09-08T10:00:12.150Z',
    latency_ms: 7370,
    input_tokens: 3842,
    output_tokens: 512,
    cost_usd: 0.0023,
  },
  {
    run_id: MOCK_RUN_ID,
    trace_id: 'trace-001',
    event_seq: 4,
    node: 'judge',
    status_before: 'RUNNING',
    status_after: 'RUNNING',
    model_id: 'gemini-2.5-flash-nothink',
    started_at: '2025-09-08T10:00:12.150Z',
    ended_at: '2025-09-08T10:00:24.800Z',
    latency_ms: 12650,
    input_tokens: 5120,
    output_tokens: 1024,
    cost_usd: 0.0051,
  },
  {
    run_id: MOCK_RUN_ID,
    trace_id: 'trace-001',
    event_seq: 5,
    node: 'validator',
    status_before: 'RUNNING',
    status_after: 'SUCCEEDED',
    model_id: 'gemini-2.5-flash-nothink',
    started_at: '2025-09-08T10:00:24.800Z',
    ended_at: '2025-09-08T10:00:31.200Z',
    latency_ms: 6400,
    input_tokens: 2048,
    output_tokens: 256,
    cost_usd: 0.0012,
  },
];

/**
 * Artifact payloads match projection.py output.
 * - retrieved_chunks / reranked_chunks use { chunks: [...] }
 * - Each chunk has: rank, asset_id, ticker, source_type, reference_date, score, headline, snippet
 * - reranked_chunks additionally have rerank_score
 */
export const MOCK_ARTIFACTS: ArtifactResponse[] = [
  // --- Miner: retrieved_chunks (hybrid BM25 + vector, top-20 via RRF) ---
  {
    run_id: MOCK_RUN_ID,
    event_seq: 1,
    node: 'miner',
    artifact_type: 'retrieved_chunks',
    created_at: '2025-09-08T10:00:02.340Z',
    payload: {
      chunks: [
        {
          rank: 1,
          asset_id: 'polygon_news:reuters:aapl-q3-2025-earnings',
          ticker: 'AAPL',
          source_type: 'polygon_news',
          reference_date: '2025-09-08',
          score: 0.92,
          headline: 'Apple Q3 2025 Earnings Beat',
          snippet: 'Apple reported Q3 2025 revenue of $94.8B, beating analyst expectations of $92.1B. iPhone revenue grew 8% YoY driven by strong demand for the iPhone 16 Pro series.',
        },
        {
          rank: 2,
          asset_id: 'polygon_news:wsj:fed-rate-pause-sep2025',
          ticker: 'AAPL',
          source_type: 'polygon_news',
          reference_date: '2025-09-08',
          score: 0.85,
          headline: 'Fed Signals Rate Pause',
          snippet: 'The Federal Reserve signaled a pause in rate cuts, keeping rates at 4.25-4.50%. Markets reacted negatively as hopes for continued easing diminished.',
        },
        {
          rank: 3,
          asset_id: 'polygon_news:bloomberg:vision-pro-q3-miss',
          ticker: 'AAPL',
          source_type: 'polygon_news',
          reference_date: '2025-09-07',
          score: 0.78,
          headline: 'Apple Vision Pro Sales Disappoint',
          snippet: 'Apple Vision Pro sales fell short of internal targets, with only 200K units shipped in Q3. Management indicated a pivot toward enterprise use cases.',
        },
        {
          rank: 4,
          asset_id: 'fmp_news:marketwatch:aapl-services-growth',
          ticker: 'AAPL',
          source_type: 'fmp_news',
          reference_date: '2025-09-08',
          score: 0.71,
          headline: 'Apple Services Revenue Hits Record $24.2B',
          snippet: 'Services segment posted record quarterly revenue of $24.2B, up 14% YoY. App Store, iCloud, and Apple Music all contributed to the growth.',
        },
        {
          rank: 5,
          asset_id: 'finnhub_company_news:cnbc:aapl-china-risk',
          ticker: 'AAPL',
          source_type: 'finnhub_company_news',
          reference_date: '2025-09-07',
          score: 0.65,
          headline: 'China Revenue Pressures Persist for Apple',
          snippet: 'Greater China revenue declined 4% YoY to $15.2B amid intensifying competition from Huawei and local smartphone makers.',
        },
        {
          rank: 6,
          asset_id: 'fmp_fundamentals:aapl:q3-2025',
          ticker: 'AAPL',
          source_type: 'fmp_fundamentals',
          reference_date: '2025-09-08',
          score: 0.58,
          headline: 'AAPL Q3 2025 Financial Summary',
          snippet: 'EPS $1.47 vs est. $1.39. Gross margin 46.3%. Operating cash flow $28.9B. Share buyback authorization increased by $90B.',
        },
        {
          rank: 7,
          asset_id: 'polygon_news:reuters:tech-sector-rotation',
          ticker: 'AAPL',
          source_type: 'polygon_news',
          reference_date: '2025-09-06',
          score: 0.42,
          headline: 'Tech Sector Rotation Accelerates',
          snippet: 'Institutional investors rotated out of large-cap tech into value and small-cap sectors amid rising Treasury yields and valuation concerns.',
        },
      ],
    },
  },

  // --- Miner: reranked_chunks (cross-encoder reranked, top-8) ---
  {
    run_id: MOCK_RUN_ID,
    event_seq: 2,
    node: 'miner',
    artifact_type: 'reranked_chunks',
    created_at: '2025-09-08T10:00:04.780Z',
    payload: {
      chunks: [
        {
          rank: 1,
          asset_id: 'polygon_news:reuters:aapl-q3-2025-earnings',
          ticker: 'AAPL',
          source_type: 'polygon_news',
          reference_date: '2025-09-08',
          score: 0.92,
          headline: 'Apple Q3 2025 Earnings Beat',
          snippet: 'Apple reported Q3 2025 revenue of $94.8B, beating analyst expectations of $92.1B. iPhone revenue grew 8% YoY.',
          rerank_score: 0.97,
        },
        {
          rank: 2,
          asset_id: 'polygon_news:wsj:fed-rate-pause-sep2025',
          ticker: 'AAPL',
          source_type: 'polygon_news',
          reference_date: '2025-09-08',
          score: 0.85,
          headline: 'Fed Signals Rate Pause',
          snippet: 'The Federal Reserve signaled a pause in rate cuts, keeping rates at 4.25-4.50%.',
          rerank_score: 0.94,
        },
        {
          rank: 3,
          asset_id: 'polygon_news:bloomberg:vision-pro-q3-miss',
          ticker: 'AAPL',
          source_type: 'polygon_news',
          reference_date: '2025-09-07',
          score: 0.78,
          headline: 'Apple Vision Pro Sales Disappoint',
          snippet: 'Apple Vision Pro sales fell short of internal targets, with only 200K units shipped in Q3.',
          rerank_score: 0.89,
        },
        {
          rank: 4,
          asset_id: 'fmp_fundamentals:aapl:q3-2025',
          ticker: 'AAPL',
          source_type: 'fmp_fundamentals',
          reference_date: '2025-09-08',
          score: 0.58,
          headline: 'AAPL Q3 2025 Financial Summary',
          snippet: 'EPS $1.47 vs est. $1.39. Gross margin 46.3%. Operating cash flow $28.9B.',
          rerank_score: 0.82,
        },
      ],
    },
  },

  // --- Critic: graded_evidence ---
  {
    run_id: MOCK_RUN_ID,
    event_seq: 3,
    node: 'critic',
    artifact_type: 'graded_evidence',
    created_at: '2025-09-08T10:00:11.000Z',
    payload: {
      items: [
        {
          chunk_id: 'polygon_news:reuters:aapl-q3-2025-earnings',
          relevance: 'HIGH',
          category: 'earnings',
          temporal_match: true,
          reasoning: 'Direct earnings report matching the trade date with specific revenue figures and YoY growth metrics.',
          event_specificity: 'HIGH',
          temporal_alignment: 'EXACT',
          evidence_granularity: 'QUANTITATIVE',
          conflict_signal: false,
          original_relevance: 'HIGH',
        },
        {
          chunk_id: 'polygon_news:wsj:fed-rate-pause-sep2025',
          relevance: 'HIGH',
          category: 'macro_policy',
          temporal_match: true,
          reasoning: 'Fed rate decision directly impacts equity valuations via discount rate mechanism.',
          event_specificity: 'HIGH',
          temporal_alignment: 'EXACT',
          evidence_granularity: 'QUALITATIVE',
          conflict_signal: false,
          original_relevance: 'HIGH',
        },
        {
          chunk_id: 'polygon_news:bloomberg:vision-pro-q3-miss',
          relevance: 'MEDIUM',
          category: 'product',
          temporal_match: true,
          reasoning: 'Product-specific sales miss, but Vision Pro is a small revenue contributor relative to iPhone.',
          event_specificity: 'MEDIUM',
          temporal_alignment: 'NEAR',
          evidence_granularity: 'QUANTITATIVE',
          conflict_signal: false,
          original_relevance: 'MEDIUM',
        },
      ],
    },
  },

  // --- Critic: critic_decision ---
  {
    run_id: MOCK_RUN_ID,
    event_seq: 3,
    node: 'critic',
    artifact_type: 'critic_decision',
    created_at: '2025-09-08T10:00:12.150Z',
    payload: {
      decision: {
        verdict: 'SUFFICIENT',
        reasoning: 'Evidence includes earnings beat (+2.9% vs consensus), Fed rate pause, and Vision Pro shortfall. These provide sufficient causal links to the -2.50% price movement.',
        high_relevance_count: 2,
        medium_relevance_count: 1,
        low_relevance_count: 0,
      },
    },
  },

  // --- Judge: judge_causes ---
  {
    run_id: MOCK_RUN_ID,
    event_seq: 4,
    node: 'judge',
    artifact_type: 'judge_causes',
    created_at: '2025-09-08T10:00:24.800Z',
    payload: {
      causes: [
        {
          title: 'Fed Rate Pause Signal',
          weight: 0.45,
          direction: 'negative',
          summary: 'Market had priced in continued easing; pause dashed expectations for cheaper borrowing costs.',
          evidence_ids: ['polygon_news:wsj:fed-rate-pause-sep2025'],
        },
        {
          title: 'Vision Pro Sales Miss',
          weight: 0.30,
          direction: 'negative',
          summary: 'Flagship product underperformed internal targets, raising concerns about innovation pipeline.',
          evidence_ids: ['polygon_news:bloomberg:vision-pro-q3-miss'],
        },
        {
          title: 'Earnings Beat (Partial Offset)',
          weight: 0.25,
          direction: 'positive',
          summary: 'Revenue of $94.8B beat consensus by $2.7B but was insufficient to offset macro headwinds.',
          evidence_ids: ['polygon_news:reuters:aapl-q3-2025-earnings'],
        },
      ],
    },
  },

  // --- Judge: judge_summary ---
  {
    run_id: MOCK_RUN_ID,
    event_seq: 4,
    node: 'judge',
    artifact_type: 'judge_summary',
    created_at: '2025-09-08T10:00:24.800Z',
    payload: {
      summary_md: '## AAPL -2.50% on 2025-09-08\n\nThe decline was primarily driven by the Federal Reserve\'s unexpected rate pause signal (45% weight) and Apple Vision Pro\'s disappointing sales (30% weight). While Q3 earnings beat expectations, the macro headwinds outweighed company-specific positives.',
      grounding_rate: 0.87,
    },
  },

  // --- Validator: validator_decision ---
  {
    run_id: MOCK_RUN_ID,
    event_seq: 5,
    node: 'validator',
    artifact_type: 'validator_decision',
    created_at: '2025-09-08T10:00:31.200Z',
    payload: {
      output_status: 'SUCCEEDED',
      validation_error: null,
      validator_attempts: 1,
    },
  },

  // --- Raw LLM Responses ---
  {
    run_id: MOCK_RUN_ID,
    event_seq: 3,
    node: 'critic',
    artifact_type: 'raw_llm_response',
    created_at: '2025-09-08T10:00:12.150Z',
    payload: {
      text: '{"verdict":"SUFFICIENT","reasoning":"Evidence pool contains direct earnings data and macro context."}',
    },
  },
  {
    run_id: MOCK_RUN_ID,
    event_seq: 4,
    node: 'judge',
    artifact_type: 'raw_llm_response',
    created_at: '2025-09-08T10:00:24.800Z',
    payload: {
      text: '{"causes":[{"title":"Fed Rate Pause","weight":0.45}],"summary":"Decline driven by macro headwinds."}',
    },
  },
  {
    run_id: MOCK_RUN_ID,
    event_seq: 5,
    node: 'validator',
    artifact_type: 'raw_llm_response',
    created_at: '2025-09-08T10:00:31.200Z',
    payload: {
      text: '{"output_status":"SUCCEEDED","schema_valid":true,"weights_sum":1.0}',
    },
  },
];
