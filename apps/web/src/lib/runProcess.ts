import { newsIdFromEvidenceId } from './answer';
import type { components } from '@/api/schema';
import type {
  EvidenceDecision,
  ProcessModel,
  ProcessSearch,
  ProcessStage,
  ProposedCause,
  QuoteCheck,
  RuleCheck,
  VerifierResult,
} from '../features/workbench/answer/model';
import { arr, isRecord, num, str } from './guards';

type RunViewDTO = components['schemas']['RunViewDTO'];

const stageLabels: Record<string, ProcessStage['label']> = {
  research_and_context_pack: 'Retrieve',
  evidence_analyst: 'Assess',
  checks_and_verifier: 'Check + verify',
  claims_and_render: 'Write',
};

const quoteRuleText: Record<string, string> = {
  R1_NOT_SUPPORTING_PACK_EVIDENCE: 'evidence does not support the hypothesis',
  R2_NOT_VERBATIM: 'quote is not verbatim',
  R2_NOT_COMPLETE_SENTENCE: 'not a complete sentence',
  R2_TOO_MANY_SENTENCES: 'too many sentences',
  R2_NOT_PROSE: 'not prose',
  R2_NOT_COMPLETE_UNIT: 'not a complete document unit',
  R3_NOT_FULL_TEXT: 'source is not full text',
  R4_OUTSIDE_SESSION_WINDOW: 'outside the session window',
  R4a_OUTSIDE_PUBLICATION_WINDOW: 'outside the publication window',
  R4b_EVENT_TIME_UNVERIFIED: 'event time is unverified',
  R4b_EVENT_TIME_CONTRADICTION: 'event time contradicts the quote',
  R5_QUOTE_LENGTH: 'quote is too long',
  R5_ANSWER_LENGTH: 'answer is too long',
  R5_DUPLICATE_QUOTE: 'duplicate quote',
  R5_STATEMENT_LENGTH: 'statement is too long',
  R6_OTHER_SESSION_MOVE: 'describes a move from another session',
  R8_HEDGED_LINK: 'causal link is hedged',
};

function recordList(value: unknown): Record<string, unknown>[] {
  return arr(value).filter(isRecord);
}

function shortenedTitle(title: string): string {
  const words = title.trim().split(/\s+/).filter(Boolean);
  return words.length > 6 ? `${words.slice(0, 6).join(' ')}…` : words.join(' ');
}

function sourceFor(
  evidenceId: string,
  sources: Record<string, unknown>[],
  newsTitles?: ReadonlyMap<string, string>,
): string {
  const newsId = newsIdFromEvidenceId(evidenceId);
  const title = newsId ? newsTitles?.get(newsId) : undefined;
  const source = sources.find((item) => str(item.evidence_id) === evidenceId);
  const publisher = source ? str(source.publisher) : null;
  if (title) return `${publisher ?? 'Web page'} · ${shortenedTitle(title)}`;
  return publisher ?? 'Web page';
}

function displayDisposition(value: string | null): EvidenceDecision['disposition'] {
  if (value === 'SUPPORT') return 'Supports';
  if (value?.startsWith('CONTRADICT')) return 'Contradicts';
  if (value === 'WEAK') return 'Weak';
  return 'Not relevant';
}

function decisionSummary(decisions: EvidenceDecision[]): string | null {
  const counts: Array<[EvidenceDecision['disposition'], string]> = [
    ['Supports', 'support'],
    ['Contradicts', 'contradict'],
    ['Weak', 'weak'],
    ['Not relevant', 'not relevant'],
  ];
  const summary = counts.flatMap(([disposition, label]) => {
    const count = decisions.filter((decision) => decision.disposition === disposition).length;
    return count ? [`${count} ${label}`] : [];
  });
  return summary.length ? summary.join(', ') : null;
}

// Runs recorded before the trace carried evidence_need fall back to "Search n".
const searchNeeds: Record<string, string> = {
  COMPANY_PRIMARY: 'Company filings and releases',
  COMPANY_NEWS: 'Company news',
  SECTOR_NEWS: 'Sector and peers',
  MACRO_EVENT: 'Macro events',
  MACRO_SERIES: 'Macro data',
  FUNDAMENTALS: 'Fundamentals',
  MARKET_STRUCTURE: 'Market structure',
  LEAD_FOLLOW_UP: 'Lead follow-up',
};

function searchLabel(need: string | null, timeScope: string | null): string | null {
  const label = need ? searchNeeds[need] : undefined;
  if (!label) return null;
  return timeScope === 'PRIOR_SESSION' ? `${label} · prior session` : label;
}

function mapSearches(retrieval: Record<string, unknown> | null): ProcessSearch[] {
  if (!retrieval) return [];
  return recordList(retrieval.tasks).map((task, index) => {
    const mode = str(task.mode_served);
    const normalizedMode = mode?.toLowerCase().replace(/_/g, ' ') ?? '';
    const source =
      mode === 'fts5'
        ? 'full-text index'
        : mode === 'web' || mode === 'tavily'
          ? 'web search'
          : normalizedMode || 'unknown';
    return {
      label: searchLabel(str(task.evidence_need), str(task.time_scope)) ?? `Search ${index + 1}`,
      source,
      items: num(task.items) ?? 0,
      seconds: num(task.latency_ms) === null ? null : (num(task.latency_ms) as number) / 1000,
      failed: str(task.status) !== 'SUCCEEDED',
    };
  });
}

function makeRetrievalSummary(retrieval: Record<string, unknown> | null): string | null {
  if (!retrieval) return null;
  const inventory = num(retrieval.inventory);
  const included = num(retrieval.included_in_pack);
  if (inventory === null || included === null) return null;
  const states = isRecord(retrieval.included_by_content_state) ? retrieval.included_by_content_state : {};
  const fullText = num(states.FULL_TEXT) ?? 0;
  const allFullText = fullText === included;
  return `${inventory} found · ${included} in pack · ${allFullText ? 'all full text' : `${fullText} full text`}`;
}

function mapDecisions(
  trace: Record<string, unknown>,
  sources: Record<string, unknown>[],
  newsTitles?: ReadonlyMap<string, string>,
): EvidenceDecision[] {
  const rounds = recordList(trace.rounds);
  const lastRound = rounds[rounds.length - 1];
  const modelReported = lastRound && isRecord(lastRound.model_reported) ? lastRound.model_reported : null;
  const analyst = modelReported && isRecord(modelReported.analyst) ? modelReported.analyst : null;
  // The desk's per-item extraction says in one line what each item reports; it labels items whose
  // title is not in the news list (most web pages and filings).
  const desk = lastRound && isRecord(lastRound.desk_decision) ? lastRound.desk_decision : null;
  const summaries = new Map<string, string>();
  for (const extraction of recordList(desk?.extractions)) {
    const id = str(extraction.evidence_id);
    const fields = isRecord(extraction.fields) ? extraction.fields : null;
    const summary = fields ? str(fields.event_summary) : null;
    if (id && summary && !summaries.has(id)) summaries.set(id, summary);
  }
  return recordList(analyst?.evidence_decisions).flatMap((decision) => {
    const id = str(decision.evidence_id);
    if (id === null) return [];
    return [
      {
        id,
        newsId: newsIdFromEvidenceId(id),
        disposition: displayDisposition(str(decision.disposition)),
        source: sourceFor(id, sources, newsTitles),
        note: str(decision.note) ?? summaries.get(id) ?? '',
      },
    ];
  });
}

function mapCauses(trace: Record<string, unknown>): ProposedCause[] {
  const rounds = recordList(trace.rounds);
  const lastRound = rounds[rounds.length - 1];
  const reported = lastRound && isRecord(lastRound.model_reported) ? lastRound.model_reported : null;
  const analyst = reported && isRecord(reported.analyst) ? reported.analyst : null;
  return recordList(analyst?.hypotheses).flatMap((hypothesis) => {
    const role = str(hypothesis.proposed_role);
    const fit = str(hypothesis.magnitude_fit);
    const roleMap: Record<string, ProposedCause['role']> = {
      PRIMARY: 'Primary',
      SECONDARY: 'Secondary',
      CONTEXT: 'Context',
    };
    const fitMap: Record<string, string> = {
      STRONG: 'strong match',
      MODERATE: 'plausible',
      PLAUSIBLE: 'plausible',
      WEAK: 'weak match',
    };
    const quotes = arr(hypothesis.quotes);
    return [
      {
        role: roleMap[role ?? ''] ?? 'Context',
        fit: fitMap[fit ?? ''] ?? 'plausible',
        quoteCount: quotes.length,
        statement: str(hypothesis.statement) ?? '',
      },
    ];
  });
}

function mapQuoteChecks(trace: Record<string, unknown>): QuoteCheck[] {
  const rounds = recordList(trace.rounds);
  const lastRound = rounds[rounds.length - 1];
  const codeChecks = lastRound && isRecord(lastRound.code_checks) ? lastRound.code_checks : null;
  return recordList(codeChecks?.quotes).flatMap((quote) => {
    const rule = str(quote.rule);
    return [
      {
        text: str(quote.text) ?? '',
        kept: quote.kept === true,
        reason:
          quote.kept === true
            ? null
            : `Rejected: ${quoteRuleText[rule ?? ''] ?? 'quote failed a validation rule'}`,
      },
    ];
  });
}

function mapRules(trace: Record<string, unknown>, ticker: string | null): RuleCheck[] {
  const rounds = recordList(trace.rounds);
  const lastRound = rounds[rounds.length - 1];
  const checks = lastRound && isRecord(lastRound.code_checks) ? lastRound.code_checks : null;
  const labels: Record<string, string> = {
    comove: 'Moves with peers',
    subject: `About ${ticker ?? 'the target'}`,
    timing: 'Inside the window',
  };
  return recordList(checks?.gates).flatMap((gate): RuleCheck[] => {
    const name = str(gate.gate);
    if (name === null || labels[name] === undefined) return [];
    return [
      {
        label: labels[name],
        result: str(gate.result) === 'no_change' ? 'Passed' : 'Changed the answer',
        detail: str(gate.detail),
      },
    ];
  });
}

function mapVerifier(trace: Record<string, unknown>): VerifierResult | null {
  const rounds = recordList(trace.rounds);
  const lastRound = rounds[rounds.length - 1];
  const reported = lastRound && isRecord(lastRound.model_reported) ? lastRound.model_reported : null;
  if (!reported || !isRecord(reported.verifier)) return null;
  const verifier = reported.verifier;
  if (verifier.called === false) return { verdict: 'Not run', reason: null };
  const raw = str(verifier.verdict);
  const verdict =
    raw === 'SUPPORTED'
      ? 'Supported'
      : raw === 'NOT_SUPPORTED' || raw === 'REJECTED'
        ? 'Not supported'
        : raw
          ? raw
              .toLowerCase()
              .replace(
                /(^|_)([a-z])/g,
                (_match, prefix: string, letter: string) => `${prefix ? ' ' : ''}${letter.toUpperCase()}`,
              )
          : 'Not run';
  return { verdict, reason: str(verifier.reason) };
}

function countSummary(checks: QuoteCheck[]): string | null {
  if (!checks.length) return null;
  const kept = checks.filter((check) => check.kept).length;
  const rejected = checks.length - kept;
  return `${kept} kept, ${rejected} rejected`;
}

export function toProcessModel(view: RunViewDTO, newsTitles?: ReadonlyMap<string, string>): ProcessModel {
  const trace = isRecord(view.trace) ? view.trace : null;
  if (!trace) {
    return {
      totalSeconds: null,
      stages: [],
      searches: [],
      retrievalSummary: null,
      decisions: [],
      decisionSummary: null,
      causes: [],
      quoteChecks: [],
      quoteSummary: null,
      rules: [],
      verifier: null,
      traceJson: null,
    };
  }
  const retrieval = isRecord(trace.retrieval) ? trace.retrieval : null;
  const answerSources =
    view.answer && Array.isArray(view.answer.sources) ? view.answer.sources.filter(isRecord) : [];
  const rawStages = recordList(trace.stages).flatMap((stage): ProcessStage[] => {
    const stageName = str(stage.stage);
    const label = stageName ? stageLabels[stageName] : undefined;
    const start = num(stage.started_ms);
    const duration = num(stage.duration_ms);
    return label === undefined || start === null || duration === null
      ? []
      : [
          {
            label,
            startSeconds: start / 1000,
            durationSeconds: duration / 1000,
          },
        ];
  });
  const assessStages = rawStages.filter((stage) => stage.label === 'Assess');
  const stages = rawStages.filter((stage) => stage.label !== 'Assess');
  if (assessStages.length) {
    stages.push({
      label: 'Assess',
      startSeconds: Math.min(...assessStages.map((stage) => stage.startSeconds)),
      durationSeconds: assessStages.reduce((sum, stage) => sum + stage.durationSeconds, 0),
    });
    stages.sort((left, right) => left.startSeconds - right.startSeconds);
  }
  const decisions = mapDecisions(trace, answerSources, newsTitles);
  const quoteChecks = mapQuoteChecks(trace);
  return {
    totalSeconds: num(trace.total_ms) === null ? null : (num(trace.total_ms) as number) / 1000,
    stages,
    searches: mapSearches(retrieval),
    retrievalSummary: makeRetrievalSummary(retrieval),
    decisions,
    decisionSummary: decisionSummary(decisions),
    causes: mapCauses(trace),
    quoteChecks,
    quoteSummary: countSummary(quoteChecks),
    rules: mapRules(trace, view.run.ticker ?? null),
    verifier: mapVerifier(trace),
    traceJson: JSON.stringify(view.trace, null, 2),
  };
}
