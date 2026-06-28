export type DemoCaseId = 'SUFFICIENT' | 'PARTIAL' | 'INSUFFICIENT';
export type AttributionStatus = DemoCaseId | 'SYSTEM_ERROR';
export type EvidenceQuality = 'high' | 'medium' | 'low';
export type EvidenceDecision = 'accepted' | 'rejected' | 'ungraded';
export type TemporalStatus = 'same-day' | 'prior-window' | 'outside-window';
export type PipelineStepStatus = 'pending' | 'active' | 'complete' | 'warning' | 'skipped' | 'error';
export type CauseDirection = 'positive' | 'negative' | 'mixed' | 'neutral';
export type CauseRole = 'primary_driver' | 'secondary_driver' | 'supporting_context' | 'weak_context';
export type SupportLevel = 'strong' | 'moderate' | 'weak';

export interface Candle {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface EvidenceItem {
  id: string;
  title: string;
  snippet: string;
  source: string;
  sourceType: 'news' | 'filing' | 'fundamentals' | 'macro';
  publishedAt: string;
  quality: EvidenceQuality;
  temporalStatus: TemporalStatus;
  relevanceScore: number;
  criticDecision: EvidenceDecision;
}

export interface AttributionCause {
  title: string;
  direction: CauseDirection;
  role: CauseRole;
  supportLevel: SupportLevel;
  confidence: number;
  rationale: string;
  evidenceIds: string[];
}

export interface AttributionResult {
  status: AttributionStatus;
  label: string;
  headline: string;
  summary: string;
  statusReason: string;
  groundingRate: number;
  refused: boolean;
  causes: AttributionCause[];
}

export const PIPELINE_STEP_IDS = [
  'query',
  'window',
  'retrieval',
  'miner',
  'critic',
  'router',
  'judge',
  'validator',
  'finalized',
] as const;

export type PipelineStepId = (typeof PIPELINE_STEP_IDS)[number];

export interface PipelineStep {
  id: PipelineStepId;
  label: string;
  status: PipelineStepStatus;
  note: string;
  durationMs?: number;
}

export interface DemoCase {
  id: DemoCaseId;
  scenarioName: string;
  ticker: string;
  companyName: string;
  defaultSelectedDate: string;
  movePct: number;
  mode: 'DEMO';
  candles: Candle[];
  candidateEvidence: EvidenceItem[];
  attributionResult: AttributionResult;
  pipelineBeforeRun: PipelineStep[];
  pipelineRunning: PipelineStep[];
  pipelineCompleted: PipelineStep[];
}

const STEP_LABELS: Record<PipelineStepId, string> = {
  query: 'Query Parsed',
  window: 'Event Window',
  retrieval: 'Retrieval',
  miner: 'Miner',
  critic: 'Critic',
  router: 'Router',
  judge: 'Judge',
  validator: 'Validator',
  finalized: 'Finalized',
};

function makeCandles(startDate: string, closes: number[], volumes: number[]): Candle[] {
  const start = new Date(`${startDate}T00:00:00Z`);
  return closes.map((close, index) => {
    const date = new Date(start);
    date.setUTCDate(start.getUTCDate() + index);
    const previous = index === 0 ? close - 0.8 : closes[index - 1];
    const open = previous + (index % 2 === 0 ? 0.35 : -0.25);
    return {
      date: date.toISOString().slice(0, 10),
      open,
      high: Math.max(open, close) + 1.15 + (index % 3) * 0.2,
      low: Math.min(open, close) - 0.95 - (index % 2) * 0.25,
      close,
      volume: volumes[index] * 1_000_000,
    };
  });
}

function makePipeline(
  outcome: DemoCaseId,
  criticNote: string,
  judgeNote: string,
): PipelineStep[] {
  return PIPELINE_STEP_IDS.map((id, index) => {
    let status: PipelineStepStatus = 'complete';
    if (outcome === 'PARTIAL' && id === 'critic') status = 'warning';
    if (outcome === 'INSUFFICIENT' && id === 'critic') status = 'warning';
    if (outcome === 'INSUFFICIENT' && id === 'judge') status = 'skipped';

    const notes: Record<PipelineStepId, string> = {
      query: 'Ticker and date normalized',
      window: 'Five-day evidence window set',
      retrieval: outcome === 'INSUFFICIENT' ? 'Two weak candidates returned' : 'Curated evidence ranked',
      miner: 'Candidate drivers extracted',
      critic: criticNote,
      router: outcome === 'INSUFFICIENT' ? 'Refusal route selected' : 'Evidence sent to judge',
      judge: judgeNote,
      validator: outcome === 'INSUFFICIENT' ? 'Refusal contract validated' : 'Citations and weights checked',
      finalized: outcome === 'INSUFFICIENT' ? 'Insufficient evidence response' : `${outcome.toLowerCase()} attribution ready`,
    };

    return {
      id,
      label: STEP_LABELS[id],
      status,
      note: notes[id],
      durationMs: id === 'finalized' ? undefined : 180 + index * 137,
    };
  });
}

function makePipelineBeforeRun(): PipelineStep[] {
  return PIPELINE_STEP_IDS.map((id) => ({
    id,
    label: STEP_LABELS[id],
    status: 'pending' as PipelineStepStatus,
    note: 'Waiting to run',
  }));
}

function makePipelineRunning(): PipelineStep[] {
  return PIPELINE_STEP_IDS.map((id, index) => ({
    id,
    label: STEP_LABELS[id],
    status: (index < 3 ? 'complete' : index === 3 ? 'active' : 'pending') as PipelineStepStatus,
    note: index < 3 ? 'Completed' : index === 3 ? 'Processing' : 'Waiting',
  }));
}

// ── Evidence fixtures with explicit criticDecision ──

const sufficientEvidence: EvidenceItem[] = [
  {
    id: 'ev-nvda-guidance',
    title: 'NVIDIA raises Q3 revenue guidance above consensus',
    snippet: 'NVIDIA Corporation announced updated fiscal Q3 guidance with revenue expected between $16.0B and $16.8B, above the prior consensus of $12.6B.',
    source: 'NVIDIA Corp',
    sourceType: 'filing',
    publishedAt: '2025-06-10T13:30:00Z',
    quality: 'high',
    temporalStatus: 'same-day',
    relevanceScore: 0.94,
    criticDecision: 'accepted',
  },
  {
    id: 'ev-nvda-filing',
    title: 'NVIDIA 8-K filing confirms raised guidance',
    snippet: 'The company filed an 8-K with the SEC confirming the preliminary revenue guidance update, citing strong data center demand.',
    source: 'SEC EDGAR',
    sourceType: 'filing',
    publishedAt: '2025-06-10T14:45:00Z',
    quality: 'high',
    temporalStatus: 'same-day',
    relevanceScore: 0.91,
    criticDecision: 'accepted',
  },
  {
    id: 'ev-chip-sector',
    title: 'Semiconductor sector rallies on AI demand optimism',
    snippet: 'The Philadelphia Semiconductor Index rose 3.2% as multiple analysts raised price targets across the sector, citing accelerating AI infrastructure investment.',
    source: 'Reuters',
    sourceType: 'macro',
    publishedAt: '2025-06-10T11:00:00Z',
    quality: 'medium',
    temporalStatus: 'same-day',
    relevanceScore: 0.68,
    criticDecision: 'accepted',
  },
  {
    id: 'ev-nvda-market',
    title: 'Broad market rally lifts technology shares',
    snippet: 'US equity markets advanced broadly with the S&P 500 gaining 1.1%, led by technology and consumer discretionary sectors.',
    source: 'Bloomberg',
    sourceType: 'macro',
    publishedAt: '2025-06-10T16:00:00Z',
    quality: 'low',
    temporalStatus: 'same-day',
    relevanceScore: 0.45,
    criticDecision: 'rejected',
  },
];

const partialEvidence: EvidenceItem[] = [
  {
    id: 'ev-aapl-rates',
    title: 'US 10-year Treasury yield climbs to 4.35%',
    snippet: 'Treasury yields rose sharply after stronger-than-expected ISM services data, pressuring long-duration technology shares including Apple.',
    source: 'CNBC',
    sourceType: 'macro',
    publishedAt: '2025-09-08T10:15:00Z',
    quality: 'high',
    temporalStatus: 'same-day',
    relevanceScore: 0.82,
    criticDecision: 'accepted',
  },
  {
    id: 'ev-aapl-vision',
    title: 'Analyst cuts Vision Pro shipment forecast',
    snippet: 'A prominent supply chain analyst reduced calendar 2025 Vision Pro shipment estimates from 500K to 350K units, citing production issues.',
    source: 'MacRumors',
    sourceType: 'news',
    publishedAt: '2025-09-07T18:00:00Z',
    quality: 'medium',
    temporalStatus: 'prior-window',
    relevanceScore: 0.61,
    criticDecision: 'accepted',
  },
  {
    id: 'ev-aapl-services',
    title: 'Apple services revenue growth accelerates',
    snippet: 'App Store and subscription revenue growth reportedly accelerated in Q3, providing a partial offset to hardware demand concerns.',
    source: 'Financial Times',
    sourceType: 'fundamentals',
    publishedAt: '2025-09-06T09:00:00Z',
    quality: 'medium',
    temporalStatus: 'prior-window',
    relevanceScore: 0.55,
    criticDecision: 'accepted',
  },
  {
    id: 'ev-aapl-iphone',
    title: 'iPhone 16 pre-order data shows mixed demand',
    snippet: 'Early pre-order tracking indicates stronger Pro model demand but weaker base model interest compared to the prior cycle.',
    source: 'Bloomberg',
    sourceType: 'news',
    publishedAt: '2025-09-07T14:00:00Z',
    quality: 'medium',
    temporalStatus: 'prior-window',
    relevanceScore: 0.48,
    criticDecision: 'rejected',
  },
];

const insufficientEvidence: EvidenceItem[] = [
  {
    id: 'ev-tsla-sector',
    title: 'EV sector mixed as legacy automakers report delivery numbers',
    snippet: 'Several traditional automakers reported quarterly deliveries with mixed results. The read-through for Tesla remains uncertain.',
    source: 'Reuters',
    sourceType: 'news',
    publishedAt: '2025-09-20T08:00:00Z',
    quality: 'low',
    temporalStatus: 'prior-window',
    relevanceScore: 0.35,
    criticDecision: 'rejected',
  },
  {
    id: 'ev-tsla-reg',
    title: 'NHTSA opens preliminary investigation into automated driving feature',
    snippet: 'The National Highway Traffic Safety Administration has opened a preliminary evaluation into reports of unexpected braking events.',
    source: 'NHTSA',
    sourceType: 'news',
    publishedAt: '2025-09-19T12:00:00Z',
    quality: 'medium',
    temporalStatus: 'prior-window',
    relevanceScore: 0.42,
    criticDecision: 'rejected',
  },
];

// ── Demo cases ──

export const DEMO_CASES: DemoCase[] = [
  {
    id: 'SUFFICIENT',
    scenarioName: 'NVDA raised guidance',
    ticker: 'NVDA',
    companyName: 'NVIDIA Corporation',
    defaultSelectedDate: '2025-06-10',
    movePct: 8.3,
    mode: 'DEMO',
    candles: makeCandles('2025-05-30', [876.5, 880.2, 878.9, 884.1, 882.3, 886.7, 885.1, 890.4, 889.8, 892.5, 895.2, 968.9], [52, 48, 45, 51, 44, 49, 47, 53, 50, 56, 58, 112]),
    candidateEvidence: sufficientEvidence,
    attributionResult: {
      status: 'SUFFICIENT',
      label: 'Sufficient Attribution',
      headline: 'Revenue guidance revision and sector momentum drove the session.',
      summary: 'A same-day guidance raise from NVIDIA, confirmed by an SEC filing, was independently corroborated by positive semiconductor sector read-through from separate sources.',
      statusReason: 'Two independent primary sources directly support the leading driver.',
      groundingRate: 0.96,
      refused: false,
      causes: [
        {
          title: 'Revenue guidance raised above consensus',
          direction: 'positive',
          role: 'primary_driver',
          supportLevel: 'strong',
          confidence: 0.72,
          rationale: 'The revised outlook changed near-term earnings expectations and was confirmed in the company filing.',
          evidenceIds: ['ev-nvda-guidance', 'ev-nvda-filing'],
        },
        {
          title: 'Positive semiconductor read-through',
          direction: 'positive',
          role: 'supporting_context',
          supportLevel: 'moderate',
          confidence: 0.28,
          rationale: 'Peer strength reinforced that investors interpreted the update as a sector demand signal.',
          evidenceIds: ['ev-chip-sector'],
        },
      ],
    },
    pipelineBeforeRun: makePipelineBeforeRun(),
    pipelineRunning: makePipelineRunning(),
    pipelineCompleted: makePipeline('SUFFICIENT', 'Evidence set passed sufficiency checks', 'Weighted causes generated'),
  },
  {
    id: 'PARTIAL',
    scenarioName: 'AAPL guidance uncertainty',
    ticker: 'AAPL',
    companyName: 'Apple Inc.',
    defaultSelectedDate: '2025-09-08',
    movePct: -2.5,
    mode: 'DEMO',
    candles: makeCandles('2025-08-28', [236.4, 238.1, 237.5, 240.2, 239.7, 241.5, 240.8, 239.9, 241.2, 240.6, 240.9, 234.9], [48, 51, 45, 49, 44, 52, 47, 55, 50, 58, 61, 89]),
    candidateEvidence: partialEvidence,
    attributionResult: {
      status: 'PARTIAL',
      label: 'Partial Attribution',
      headline: 'Rate pressure is supported; company-specific contribution remains uncertain.',
      summary: 'The decline is partly attributable to rising Treasury yields and pressure on long-duration technology shares. Softer Vision Pro estimates may have contributed, but the report is indirect and lacks company confirmation.',
      statusReason: 'A confirming company statement and a second independent source are missing for the product-demand claim.',
      groundingRate: 0.78,
      refused: false,
      causes: [
        {
          title: 'Higher yields pressured large-cap technology',
          direction: 'negative',
          role: 'primary_driver',
          supportLevel: 'strong',
          confidence: 0.58,
          rationale: 'The timing matches the selloff and the macro source directly documents the rate move.',
          evidenceIds: ['ev-aapl-rates'],
        },
        {
          title: 'Softer Vision Pro shipment expectations',
          direction: 'negative',
          role: 'secondary_driver',
          supportLevel: 'moderate',
          confidence: 0.27,
          rationale: 'The estimate is relevant but remains unconfirmed and was published before the trading session.',
          evidenceIds: ['ev-aapl-vision'],
        },
        {
          title: 'Services strength limited downside',
          direction: 'mixed',
          role: 'weak_context',
          supportLevel: 'weak',
          confidence: 0.15,
          rationale: 'Recent fundamentals provide a modest counterweight rather than an event-day catalyst.',
          evidenceIds: ['ev-aapl-services'],
        },
      ],
    },
    pipelineBeforeRun: makePipelineBeforeRun(),
    pipelineRunning: makePipelineRunning(),
    pipelineCompleted: makePipeline('PARTIAL', 'One driver lacks independent confirmation', 'Qualified causes generated'),
  },
  {
    id: 'INSUFFICIENT',
    scenarioName: 'TSLA low-signal move',
    ticker: 'TSLA',
    companyName: 'Tesla, Inc.',
    defaultSelectedDate: '2025-09-22',
    movePct: 1.1,
    mode: 'DEMO',
    candles: makeCandles('2025-09-11', [421.2, 423.5, 419.8, 424.1, 426.3, 425.4, 427.2, 426.8, 428.4, 427.6, 429.2, 433.9], [74, 69, 82, 76, 71, 68, 73, 70, 72, 67, 75, 79]),
    candidateEvidence: insufficientEvidence,
    attributionResult: {
      status: 'INSUFFICIENT',
      label: 'Insufficient Evidence',
      headline: 'No evidence-backed company catalyst was found for the selected move.',
      summary: 'The available items are either generic sector commentary or outside the event window. Assigning a cause would exceed the available evidence.',
      statusReason: 'No timely, company-specific evidence passed the relevance and corroboration checks.',
      groundingRate: 1,
      refused: true,
      causes: [],
    },
    pipelineBeforeRun: makePipelineBeforeRun(),
    pipelineRunning: makePipelineRunning(),
    pipelineCompleted: makePipeline('INSUFFICIENT', 'Evidence failed sufficiency threshold', 'Skipped after refusal route'),
  },
];

export const DEFAULT_DEMO_CASE_ID: DemoCaseId = 'PARTIAL';

export function getDemoCase(id: DemoCaseId): DemoCase {
  return DEMO_CASES.find((demoCase) => demoCase.id === id) ?? DEMO_CASES[1];
}
