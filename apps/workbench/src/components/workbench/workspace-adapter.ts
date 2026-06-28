/**
 * Maps real API types (WorkspaceResponse, ArtifactResponse)
 * into v4 component-compatible shapes (AttributionResult, EvidenceItem, PipelineStep).
 *
 * The v4 components still import from mock/demoCases for their type definitions.
 * This adapter builds structurally compatible objects from live API data so the
 * components render correctly without demoCase imports in the LiveWorkbench path.
 */
import type { WorkspaceResponse, WorkspaceCause, WorkspaceEvidenceItem, WorkspaceStage } from '../../api/types';
import type { NewsItem } from '../../api/client';
import type {
  AttributionResult,
  EvidenceItem,
  PipelineStep,
  PipelineStepId,
  EvidenceDecision,
} from '../../mock/demoCases';

// ── Evidence mapping ──

export function mapEvidence(items: WorkspaceEvidenceItem[]): EvidenceItem[] {
  return items.map((e) => ({
    id: e.id,
    title: e.headline ?? e.id,
    snippet: e.snippet ?? '',
    source: e.source_type ?? 'news',
    sourceType: mapSourceType(e.source_type),
    publishedAt: e.reference_date ?? '',
    quality: 'medium' as const,
    temporalStatus: e.temporal_match ? 'same-day' : 'outside-window',
    relevanceScore: e.critic_relevance ?? e.retrieval_score ?? 0,
    criticDecision: (e.critic_decision as EvidenceDecision) ?? 'ungraded',
  }));
}

function mapSourceType(t: string | null | undefined): EvidenceItem['sourceType'] {
  switch (t) {
    case 'polygon_news': return 'news';
    case 'fmp_fundamentals': return 'fundamentals';
    case 'sec_filing': return 'filing';
    default: return 'news';
  }
}

// ── Result mapping ──

export function mapResult(ws: WorkspaceResponse): AttributionResult | null {
  const r = ws.result;
  if (!r) return null;

  const causes = (r.causes ?? []).map((c: WorkspaceCause) => ({
    title: c.text,
    direction: (c.direction === 'up' ? 'positive' : c.direction === 'down' ? 'negative' : 'neutral') as 'positive' | 'negative' | 'mixed' | 'neutral',
    role: 'primary_driver' as const,
    supportLevel: 'strong' as const,
    confidence: c.confidence ?? 0,
    rationale: '',
    evidenceIds: c.evidence_ids ?? [],
  }));

  return {
    status: mapStatus(ws.status),
    label: r.output_status ?? ws.status,
    statusReason: r.validation_error ?? '',
    headline: causes[0]?.title ?? 'Attribution complete',
    summary: r.summary_md ?? '',
    groundingRate: r.grounding_rate ?? 0,
    refused: ws.status === 'INSUFFICIENT' || ws.status === 'FAILED_REQUEST',
    causes,
  };
}

function mapStatus(status: string): AttributionResult['status'] {
  switch (status) {
    case 'SUCCEEDED': return 'SUFFICIENT';
    case 'PARTIAL': return 'PARTIAL';
    case 'INSUFFICIENT': return 'INSUFFICIENT';
    default: return 'INSUFFICIENT';
  }
}

// ── Pipeline step mapping ──

const PIPELINE_STAGE_TO_STEP: Record<string, PipelineStepId> = {
  miner: 'miner',
  critic: 'critic',
  decision_router: 'router',
  expand_macro: 'window',
  judge: 'judge',
  validator: 'validator',
  finalizer: 'finalized',
  insufficient_handler: 'finalized',
  system_error_handler: 'finalized',
};

const STEP_ORDER: PipelineStepId[] = [
  'query', 'window', 'retrieval', 'miner', 'critic', 'router', 'judge', 'validator', 'finalized',
];

const STEP_LABELS: Record<string, string> = {
  query: 'Query', window: 'Event Window', retrieval: 'Retrieval',
  miner: 'Retriever', critic: 'Critic', router: 'Readiness',
  judge: 'Attribution Model', validator: 'Validator', finalized: 'Finalized',
};

export function mapSteps(stages: WorkspaceStage[]): PipelineStep[] {
  const seen = new Set<string>();
  const result: PipelineStep[] = [];

  for (const stage of stages) {
    const stepId = PIPELINE_STAGE_TO_STEP[stage.id];
    if (!stepId || seen.has(stepId)) continue;
    seen.add(stepId);

    result.push({
      id: stepId,
      label: STEP_LABELS[stepId] ?? stage.label,
      status: mapStepStatus(stage.status),
      note: stage.summary ?? '',
      durationMs: stage.duration_ms ?? undefined,
    });
  }

  // Fill in any missing steps as pending
  for (const stepId of STEP_ORDER) {
    if (!seen.has(stepId)) {
      result.push({
        id: stepId,
        label: STEP_LABELS[stepId] ?? stepId,
        status: 'pending' as 'pending' | 'active' | 'complete' | 'warning' | 'skipped' | 'error',
        note: '',
      });
    }
  }

  // Sort by STEP_ORDER
  result.sort((a, b) => STEP_ORDER.indexOf(a.id) - STEP_ORDER.indexOf(b.id));
  return result;
}

function mapStepStatus(s: string): PipelineStep['status'] {
  switch (s) {
    case 'complete': return 'complete';
    case 'warning': return 'warning';
    case 'error': return 'error';
    case 'skipped': return 'skipped';
    case 'active': return 'active';
    default: return 'pending';
  }
}

// ── v4 Component Stubs (temporary — v4 components expect DemoCase shapes) ──

import type { OhlcvCandle } from '../../api/types';

// ── News → Evidence mapping (preview before attribution) ──

function stripMarkdownHeadings(s: string): string {
  return s.replace(/^#+\s.*$/gm, '').replace(/\*Source:.*$/gm, '');
}

function parseSource(sourceLine: string | null | undefined): string {
  if (!sourceLine) return 'News';
  return sourceLine.replace(/^Source:\s*/i, '').split('|')[0].trim();
}

export function mapNewsToEvidence(
  items: NewsItem[],
  selectedDate: string,
): EvidenceItem[] {
  return items.map((item) => {
    const title = item.title.replace(/^[A-Z]+:\s/, '');
    const snippet = stripMarkdownHeadings(item.snippet ?? '').trim().slice(0, 300);
    const source = parseSource(item.source_line);
    const temporalStatus: EvidenceItem['temporalStatus'] =
      item.reference_date === selectedDate ? 'same-day' : 'prior-window';

    return {
      id: item.asset_id,
      title,
      snippet,
      source,
      sourceType: 'news' as const,
      publishedAt: item.published_utc ?? `${item.reference_date}T00:00:00Z`,
      quality: 'medium' as const,
      temporalStatus,
      relevanceScore: 0,
      criticDecision: 'ungraded' as const,
    };
  });
}

/** Minimal stub satisfying v4 component Props without importing mock/demoCases at runtime. */
export interface LiveDemoCaseStub {
  id: string;
  scenarioName: string;
  ticker: string;
  companyName: string;
  candles: { date: string; open: number; high: number; low: number; close: number; volume: number }[];
  defaultSelectedDate: string;
}

export function buildDemoCaseStub(ticker: string, candles: OhlcvCandle[]): LiveDemoCaseStub {
  return {
    id: ticker,
    scenarioName: ticker,
    ticker,
    companyName: ticker,
    candles: candles.map((c) => ({
      date: c.date,
      open: c.open ?? 0,
      high: c.high ?? 0,
      low: c.low ?? 0,
      close: c.close ?? 0,
      volume: c.volume ?? 0,
    })),
    defaultSelectedDate: candles.length > 0 ? candles[Math.floor(candles.length / 2)].date : '',
  };
}

export function makeCasesList(tickers: string[]): LiveDemoCaseStub[] {
  return tickers.map((t) => buildDemoCaseStub(t, []));
}
