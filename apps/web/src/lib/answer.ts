import type { components } from '@/api/schema';
import type {
  AnswerAbstention,
  AnswerLead,
  AnswerModel,
  AnswerOutcome,
  AnswerQuote,
  RelativeRow,
} from '@/features/workbench/answer/model';
import { formatEtDateTime } from './format';
import { arr, isRecord, num, str } from './guards';

type RunViewDTO = components['schemas']['RunViewDTO'];
type Row = Record<string, unknown>;

export function newsIdFromEvidenceId(evidenceId: string): string | null {
  // "web:<hash>:web_body:0001" and "v1:asset:<hash>:web_body:0001" both name a news item by their prefix.
  const match = /^(.+?):web_body:\d+$/.exec(evidenceId);
  return match?.[1] ?? null;
}

function record(v: unknown): Row {
  return isRecord(v) ? v : {};
}

function plainTime(value: unknown): string {
  const raw = str(value);
  if (!raw) return '';
  const formatted = formatEtDateTime(raw);
  return formatted;
}

function quoteRows(value: unknown): AnswerQuote[] {
  const roleOrder = { PRIMARY: 0, SECONDARY: 1, CONTEXT: 2 } as const;
  const roleLabel = { PRIMARY: 'Primary', SECONDARY: 'Secondary', CONTEXT: 'Context' } as const;
  return arr(value)
    .filter(isRecord)
    .filter((item) => item.role === 'PRIMARY' || item.role === 'SECONDARY' || item.role === 'CONTEXT')
    .map((item, index) => ({ item, index }))
    .sort(
      (a, b) =>
        roleOrder[a.item.role as keyof typeof roleOrder] - roleOrder[b.item.role as keyof typeof roleOrder] ||
        a.index - b.index,
    )
    .map(({ item, index }) => {
      const evidenceId = str(item.evidence_id) ?? '';
      return {
        // One passage can back several quotes (e.g. primary and secondary), so the id adds role and position.
        id: `${String(item.role)}:${index}:${evidenceId}`,
        newsId: newsIdFromEvidenceId(evidenceId),
        role: roleLabel[item.role as keyof typeof roleLabel],
        text: str(item.text) ?? '',
        publisher: str(item.publisher) ?? '',
        time: plainTime(item.published_at),
        url: str(item.url),
      };
    });
}

function lead(value: unknown): AnswerLead | null {
  const item = record(value);
  const text = str(item.title) ?? str(item.headline) ?? str(item.text);
  if (text === null) return null;
  return {
    text,
    publisher: str(item.publisher) ?? '',
    time: plainTime(item.published_at),
    url: str(item.url),
  };
}

const gapText: Record<string, string> = {
  MISSING_PRIMARY_CONFIRMATION: 'No company statement confirms the cause.',
  MISSING_INDEPENDENT_CORROBORATION: 'No second, independent outlet found.',
  MISSING_PRIOR_SESSION_CONTEXT: 'No prior-session context retrieved.',
  MISSING_SECTOR_CONTEXT: 'No sector context retrieved.',
  MISSING_MACRO_CONTEXT: 'No macro context retrieved.',
  MISSING_FUNDAMENTAL_CONTEXT: 'No fundamental context retrieved.',
  CONFLICT_REQUIRES_RESOLUTION: 'Conflicting evidence needs resolution.',
  MARKET_STRUCTURE_UNSUPPORTED: 'Market-structure evidence was unavailable.',
  LOCAL_COVERAGE_GAP: 'Some sources were not available.',
};

function gapsOf(answer: Row): string[] {
  const gaps: string[] = [];
  const seen = new Set<string>();
  for (const value of arr(answer.limitations)) {
    const limitation = str(value);
    if (limitation === null) continue;
    const match = /^Coverage gap: ([A-Z][A-Z0-9_]*) for .+\.$/.exec(limitation);
    const code = match?.[1];
    const mapped = code ? (gapText[code] ?? 'Some sources were not available.') : limitation;
    if (!match && /\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b/.test(limitation)) continue;
    if (!seen.has(mapped)) {
      seen.add(mapped);
      gaps.push(mapped);
    }
  }
  return gaps;
}

export function outcomeOf(view: RunViewDTO): AnswerOutcome {
  const answer = record(view.answer);
  if (view.run.lifecycle === 'FAILED') return { label: 'Failed', tone: 'down' };
  if (view.run.lifecycle === 'CANCELLED') return { label: 'Stopped', tone: 'neutral' };
  switch (answer.outcome) {
    case 'ANSWERED':
      return { label: 'Confirmed', tone: 'up' };
    case 'ABSTAINED':
      return { label: 'No clear cause', tone: 'warn' };
    case 'TIER_ONLY':
      return { label: 'Ordinary move', tone: 'neutral' };
  }
  if (
    view.run.lifecycle === 'RUNNING' ||
    view.run.lifecycle === 'ACCEPTED' ||
    view.run.lifecycle === 'CANCEL_REQUESTED'
  ) {
    return { label: 'Running', tone: 'info' };
  }
  if (view.run.lifecycle === 'COMPLETED' && view.legacy_text && !view.answer) {
    return { label: 'Reported', tone: 'info' };
  }
  return { label: 'Completed', tone: 'neutral' };
}

// The budget guard refuses a model call that could pass the run's cost cap.
function stoppedAtCostCap(view: RunViewDTO): boolean {
  if (view.run.lifecycle !== 'FAILED') return false;
  const code = str(view.failure?.code);
  return (
    code === 'BUDGET_EXCEEDED' ||
    (code === 'SYSTEM_ERROR' && /cost ceiling/i.test(str(view.failure?.message) ?? ''))
  );
}

function failureText(view: RunViewDTO): string | null {
  if (view.run.lifecycle === 'CANCELLED') return 'This run was stopped.';
  if (view.run.lifecycle !== 'FAILED') return null;
  if (stoppedAtCostCap(view)) return 'The run reached its cost cap before finishing.';
  const code = str(view.failure?.code);
  const messages: Record<string, string> = {
    PROVIDER_UNAVAILABLE: 'The model provider was unavailable.',
    MODEL_PRICE_UNAVAILABLE: 'Model pricing was unavailable.',
    DATA_UNAVAILABLE: 'Required market data was unavailable.',
    TIMEOUT: 'The run took too long and stopped.',
    PROCESS_RECOVERY: 'The run stopped while the service was recovering.',
    SUBMISSION_FAILURE: 'The run could not be started.',
    EXECUTOR_SHUTDOWN: 'The run stopped because the service was shutting down.',
    EXECUTOR_FAILURE: 'The run stopped because the service encountered an execution error.',
    STREAM_PERSISTENCE_FAILURE: 'The run stopped because its progress could not be saved.',
    CAPACITY_EXCEEDED: 'The service could not accept the run because capacity was full.',
    CANCELLATION_INTEGRITY_FAILURE: 'The run could not be stopped safely.',
    SYSTEM_ERROR: 'The run could not be completed because of an internal error.',
  };
  return code ? (messages[code] ?? 'The run could not be completed.') : 'The run could not be completed.';
}

// The answer's key sentence is one source sentence; the hypothesis statement strings several quotes together.
function sentenceOf(value: unknown): string | null {
  return str(record(value).text);
}

function mainCauseOf(view: RunViewDTO, outcome: unknown): string | null {
  if (outcome !== 'ANSWERED') return null;
  const key = sentenceOf(record(view.answer).key_sentence);
  if (key) return key;
  const trace = record(view.trace);
  const rounds = arr(trace.rounds);
  const last = record(rounds[rounds.length - 1]);
  const report = record(last.model_reported);
  const analyst = record(report.analyst);
  const hypothesis = arr(analyst.hypotheses)
    .filter(isRecord)
    .find((item) => item.proposed_role === 'PRIMARY');
  return hypothesis ? str(hypothesis.statement) : null;
}

// The co-movement sentence ends with "SEC filing accepted <time> UTC: <url>." for each filing in the
// window; the same filings come as structured rows, so the sentence keeps only its prose.
function coMovementText(value: unknown): string | null {
  const text = str(value);
  if (!text) return null;
  return text.replace(/\s*SEC filing accepted .+? UTC: \S+?\.(?=\s|$)/g, '').trim() || null;
}

function filingRows(value: unknown): AnswerModel['filings'] {
  return arr(value).flatMap((item) => {
    const row = record(item);
    const url = str(row.url);
    const at = str(row.eligible_at);
    return url && at ? [{ url, time: formatEtDateTime(at) }] : [];
  });
}

function relativeRows(factsValue: unknown, ticker: string): RelativeRow[] {
  const facts = record(factsValue);
  const sector = record(facts.sector);
  const peers = record(facts.peers);
  const market = record(facts.market);
  const candidates: Array<[string, unknown, RelativeRow['kind']]> = [
    [ticker || 'Target', facts.target_return_pct, 'target'],
    [str(sector.ticker) ? `Sector ${str(sector.ticker)}` : 'Sector', sector.return_pct, 'sector'],
    [num(peers.count) === null ? 'Peers' : `Peers (${num(peers.count)})`, peers.median_return_pct, 'peers'],
    ['Market', market.return_pct, 'market'],
  ];
  return candidates.flatMap(([label, value, kind]) => {
    const parsed = num(value);
    return parsed === null ? [] : [{ label, value: parsed, kind }];
  });
}

function abstentionOf(answer: Row): AnswerAbstention | null {
  const abstention = record(answer.abstention);
  if (Object.keys(abstention).length === 0) return null;
  const mapLeads = (values: unknown[]): AnswerLead[] =>
    values.map(lead).filter((item): item is AnswerLead => item !== null);
  const explanations = arr(abstention.possible_explanations ?? abstention.possible_explanation);
  return {
    reason: str(abstention.reason_text) ?? '',
    possibleExplanations: mapLeads(explanations),
    leads: mapLeads(arr(abstention.leads)),
  };
}

// T0 (no close-to-close return) gets no badge.
const moveSizeLabel: Record<string, string> = {
  T1: 'Small move',
  T2: 'Moved with its group',
  T3: 'Large move',
};

export function toAnswerModel(view: RunViewDTO): AnswerModel {
  const answer = record(view.answer);
  const facts = view.answer && isRecord(view.answer.facts) ? view.answer.facts : view.facts;
  const ticker = str(view.run.ticker) ?? '';
  const relative = relativeRows(facts, ticker);
  const target = relative.find((row) => row.kind === 'target')?.value;
  const sector = relative.find((row) => row.kind === 'sector')?.value;
  const calls = isRecord(view.cost.model_calls)
    ? Object.values(view.cost.model_calls).reduce<number>((total, value) => total + (num(value) ?? 0), 0)
    : null;
  const groupContext = record(answer.group_context);
  const productTierValue = str(answer.product_tier);
  const productTier = [
    'CONFIRMED',
    'REPORTED',
    'MOVED_WITH_GROUP',
    'LARGER_THAN_GROUP',
    'NOTHING_FOUND',
    'ORDINARY_MOVE',
  ].includes(productTierValue ?? '')
    ? (productTierValue as AnswerModel['productTier'])
    : null;
  const group = Object.keys(groupContext).length
    ? {
        relation: str(groupContext.relation) ?? 'unknown',
        stockPct: num(groupContext.stock_pct),
        peersPct: num(groupContext.peers_median_pct),
        peersCount: num(groupContext.peers_count),
        sectorPct: num(groupContext.sector_pct),
        sectorTicker: str(groupContext.sector_ticker),
        residualPct: num(groupContext.residual_pct),
      }
    : null;
  return {
    runId: view.run.run_id,
    ticker,
    sessionDate: str(view.run.session_date) ?? '',
    outcome: outcomeOf(view),
    productTier,
    reported: (() => {
      const value = record(answer.reported_reason);
      const quote = str(value.quote);
      const cause = str(value.cause);
      return quote && cause
        ? {
            quote,
            cause,
            scope: str(value.scope),
            publisher: str(value.publisher),
            publishedAt: plainTime(value.published_at) || null,
            url: str(value.url),
            eventDate: str(value.event_date),
          }
        : null;
    })(),
    group,
    headline: str(answer.headline),
    mainCause: mainCauseOf(view, answer.outcome),
    alsoCause: answer.outcome === 'ANSWERED' ? sentenceOf(answer.also_sentence) : null,
    coMovement: coMovementText(record(facts).comove_sentence),
    filings: filingRows(record(facts).filings),
    quotes: quoteRows(answer.sources),
    relative,
    excessVsSector: target !== undefined && sector !== undefined ? target - sector : null,
    gaps: gapsOf(answer),
    moveSize: moveSizeLabel[view.answer?.tier ?? ''] ?? null,
    moveSizeReason:
      view.answer?.tier === 'T1' || view.answer?.tier === 'T2' ? (view.answer.tier_reason ?? null) : null,
    abstention: answer.outcome === 'ABSTAINED' ? abstentionOf(answer) : null,
    legacyText: str(view.legacy_text),
    failureMessage: failureText(view),
    stoppedAtCostCap: stoppedAtCostCap(view),
    meta: {
      model: str(view.run.model.analyst_model_id),
      provider: str(view.run.model.provider),
      costUsd: num(view.cost.cost_usd),
      costCapUsd: num(record(view.run.policy).max_cost_usd),
      maxTokens: num(record(view.cost).max_tokens),
      // A run that made no model call used no tokens; it has no accounting record to read.
      usedTokens: num(record(view.cost).used_tokens) ?? (calls === 0 ? 0 : null),
      tokensUpperBound: record(view.cost).token_method === 'upper_bound_charged',
      costHint: view.cost.cost_method === 'upper_bound_charged' ? 'upper bound' : null,
      durationSeconds: num(view.run.duration_ms) === null ? null : view.run.duration_ms! / 1000,
      // A run that stopped early has not booked its analyst calls, so its count would understate the work.
      modelCalls: failureText(view) ? null : calls,
      createdAt: str(view.run.created_at),
      finishedAt: str(view.run.finished_at),
    },
  };
}

export { toProcessModel } from './runProcess';
