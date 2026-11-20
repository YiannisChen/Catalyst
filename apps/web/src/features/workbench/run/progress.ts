import type { PublicRunEvent } from '@/api/sse';

export type RunStepStatus = 'pending' | 'active' | 'done' | 'failed';
export type RunStepId = 'accepted' | 'retrieve' | 'assess' | 'verify' | 'answer';
export type RunStep = {
  id: RunStepId;
  label: string;
  status: RunStepStatus;
  detail?: string;
  elapsed?: string;
};

export type RelativeFact = {
  label: string;
  value: number | null;
};
export type RunFacts = { headline: string; relative: RelativeFact[]; excess: number | null };
export type RunTerminal =
  | { status: 'done' }
  | { status: 'failed'; message: string; costCap?: boolean; spent?: number; cap?: number }
  | { status: 'cancelled' }
  | null;
// What the run is reading (from the latest retrieval round) and the verifier's plain-language finding.
// Both arrive before the answer, so the panel can show real content while the answer is assembled.
// read: what the article said about the move, reported live as each one is read (null until then).
export type RunReadState = 'states_cause' | 'reports_event' | 'no_cause' | 'not_about_company' | 'failed';
export type RunReadingItem = {
  evidenceId: string | null;
  publisher: string;
  publishedAt: string | null;
  url: string | null;
  read: RunReadState | null;
};
export type RunFinding = { supported: boolean; reason: string };
// The likely cause the analyst picked, shown while a second check runs (owner views only carry the quote).
export type RunCandidate = {
  quote: string;
  publisher: string | null;
  publishedAt: string | null;
  url: string | null;
};
export type RunProgress = {
  steps: RunStep[];
  facts: RunFacts | null;
  terminal: RunTerminal;
  reading: RunReadingItem[];
  finding: RunFinding | null;
  candidate: RunCandidate | null;
  readCount: number;
};

const labels: Array<Pick<RunStep, 'id' | 'label'>> = [
  { id: 'accepted', label: 'Run accepted' },
  { id: 'retrieve', label: 'Retrieve evidence' },
  { id: 'assess', label: 'Assess evidence' },
  { id: 'verify', label: 'Check quotes and verify' },
  { id: 'answer', label: 'Write answer' },
];

function elapsed(seconds: number | undefined): string | undefined {
  return seconds === undefined || !Number.isFinite(seconds) ? undefined : `${seconds.toFixed(1)} s`;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
}

function numberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** Event payloads report counts as integers; accept an array too, just in case. */
function countOf(value: unknown): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  return Array.isArray(value) ? value.length : 0;
}

function payloadOf(event: PublicRunEvent): Record<string, unknown> {
  return asRecord(event.payload);
}

function factsFrom(event: PublicRunEvent, ticker: string | null): RunFacts {
  const payload = payloadOf(event);
  const facts = asRecord(payload.facts);
  const sector = asRecord(facts.sector);
  const peers = asRecord(facts.peers);
  const market = asRecord(facts.market);
  const sectorTicker = typeof sector.ticker === 'string' ? sector.ticker : 'ETF';
  const peerCount = numberOrNull(peers.count);
  return {
    headline: typeof payload.headline === 'string' ? payload.headline : '',
    // The stock's move minus its sector ETF's move (gap_return_pct is the opening gap, not this).
    excess: (() => {
      const target = numberOrNull(facts.target_return_pct);
      const sectorMove = numberOrNull(sector.return_pct);
      return target != null && sectorMove != null ? target - sectorMove : null;
    })(),
    relative: [
      { label: ticker ?? 'Target', value: numberOrNull(facts.target_return_pct) },
      { label: `Sector ${sectorTicker}`, value: numberOrNull(sector.return_pct) },
      {
        label: peerCount === null ? 'Peers median' : `Peers (${peerCount})`,
        value: numberOrNull(peers.median_return_pct),
      },
      { label: 'Market', value: numberOrNull(market.return_pct) },
    ],
  };
}

/** A step's own duration: the payload's duration_ms, else the time since the previous step finished. */
function elapsedFor(event: PublicRunEvent, previousAt: number | undefined): string | undefined {
  const duration = payloadOf(event).duration_ms;
  if (typeof duration === 'number' && Number.isFinite(duration)) return elapsed(duration / 1000);
  const eventAt = Date.parse(event.emitted_at);
  return previousAt === undefined || !Number.isFinite(eventAt)
    ? undefined
    : elapsed(Math.max(0, eventAt - previousAt) / 1000);
}

function failedMessage(code: unknown, safeMessage: unknown): string {
  if (typeof safeMessage === 'string' && safeMessage.trim()) return safeMessage;
  const messages: Record<string, string> = {
    provider_error: 'The model provider could not complete the request.',
    timeout: 'The run took too long and stopped.',
    cancelled: 'The run was cancelled.',
    internal_error: 'The run stopped because of an internal error.',
  };
  return typeof code === 'string'
    ? (messages[code] ?? 'The run could not be completed.')
    : 'The run could not be completed.';
}

const readStates: Record<string, RunReadState> = {
  read_states_cause: 'states_cause',
  read_reports_event: 'reports_event',
  read_no_cause: 'no_cause',
  read_not_about_company: 'not_about_company',
  read_failed: 'failed',
};

export function reduceRunEvents(events: PublicRunEvent[]): RunProgress {
  const ordered = [...events].sort((a, b) => a.sequence - b.sequence);
  // Time the most recent step finished; per-step durations without duration_ms are measured from it.
  let firstAt = ordered[0] ? Date.parse(ordered[0].emitted_at) : undefined;
  const steps: RunStep[] = labels.map((step) => ({ ...step, status: 'pending' }));
  let facts: RunFacts | null = null;
  let terminal: RunTerminal = null;
  let ticker: string | null = null;
  let reading: RunReadingItem[] = [];
  let finding: RunFinding | null = null;
  let readCount = 0;
  let candidate: RunCandidate | null = null;

  const setStep = (id: RunStepId, status: RunStepStatus, detail?: string, time?: string) => {
    const step = steps.find((item) => item.id === id)!;
    if (step.status === 'done' && status === 'active') return;
    step.status = status;
    if (detail !== undefined) step.detail = detail;
    if (time !== undefined && step.elapsed === undefined) step.elapsed = time;
  };

  for (const event of ordered) {
    const payload = payloadOf(event);
    const finishedBefore = steps.filter((step) => step.status === 'done').length;
    switch (event.event_type) {
      case 'run.accepted': {
        ticker = typeof payload.ticker === 'string' ? payload.ticker : null;
        const provider =
          typeof payload.model_provider_label === 'string' ? payload.model_provider_label : 'Model';
        // The cap is shown only when the event carries it; a guessed default would be wrong.
        const cap = typeof payload.cap_usd === 'number' ? ` · cap $${payload.cap_usd.toFixed(2)}` : '';
        setStep('accepted', 'done', `${provider}${cap}`, elapsedFor(event, firstAt));
        break;
      }
      case 'stage.started': {
        const stage = payload.stage;
        const round = typeof payload.round === 'number' ? payload.round : 1;
        if (stage === 'research') setStep('retrieve', 'active');
        if (stage === 'context_pack_build' && round > 1) {
          // Nothing usable in the last round: the search widens, and the later steps run again.
          const retrieve = steps.find((item) => item.id === 'retrieve')!;
          retrieve.status = 'active';
          retrieve.detail = `Searching again (round ${round})`;
          retrieve.elapsed = undefined;
          finding = null;
          steps
            .filter((item) => item.id === 'assess' || item.id === 'verify')
            .forEach((item) => {
              item.status = 'pending';
              item.detail = undefined;
              item.elapsed = undefined;
            });
        }
        if (stage === 'desk_decision') setStep('assess', 'active', 'Reading each item');
        break;
      }
      case 'tier.decided': {
        // T0/T1 is an ordinary move: the run answers from prices alone, with no search or model call.
        if (payload.tier === 'T0' || payload.tier === 'T1') {
          const skipped = 'Not needed: an ordinary move';
          setStep('retrieve', 'done', skipped);
          setStep('assess', 'done', skipped);
          setStep('verify', 'done', skipped);
        }
        break;
      }
      case 'retrieval.done': {
        readCount = 0;
        reading = (Array.isArray(payload.items) ? payload.items : []).flatMap((item: unknown) => {
          if (!item || typeof item !== 'object') return [];
          const row = item as Record<string, unknown>;
          const publisher = typeof row.publisher === 'string' && row.publisher ? row.publisher : null;
          if (!publisher) return [];
          return [
            {
              evidenceId: typeof row.evidence_id === 'string' ? row.evidence_id : null,
              publisher,
              publishedAt: typeof row.published_at === 'string' ? row.published_at : null,
              url: typeof row.url === 'string' ? row.url : null,
              read: null,
            },
          ];
        });
        const itemCount = countOf(payload.inventory_count ?? payload.items);
        const searches =
          typeof payload.search_count === 'number' ? `${payload.search_count} searches · ` : '';
        setStep('retrieve', 'done', `${searches}${itemCount} items`, elapsedFor(event, firstAt));
        break;
      }
      case 'analyst.done': {
        const hypotheses = Array.isArray(payload.hypotheses) ? payload.hypotheses.map(asRecord) : [];
        const primary =
          hypotheses.find((h) => h.proposed_role === 'PRIMARY' && typeof h.quote === 'string') ??
          hypotheses.find((h) => typeof h.quote === 'string');
        if (primary && typeof primary.quote === 'string' && primary.quote.trim()) {
          const source = reading.find((row) => row.evidenceId && row.evidenceId === primary.evidence_id);
          candidate = {
            quote: primary.quote,
            publisher: source?.publisher ?? null,
            publishedAt: source?.publishedAt ?? null,
            url: source?.url ?? null,
          };
        }
        const decisions = countOf(payload.evidence_decisions);
        const causes = countOf(payload.hypotheses);
        setStep(
          'assess',
          'done',
          `${decisions} items · ${causes} proposed causes`,
          elapsedFor(event, firstAt),
        );
        break;
      }
      case 'analyst.decision': {
        const current = steps.find((item) => item.id === 'assess')!;
        if (current.status === 'pending') setStep('assess', 'active');
        // Live reading progress: one READ decision per article as soon as it has been read.
        if (payload.kind === 'evidence' && payload.disposition === 'READ') {
          const state = readStates[String(payload.reason_code)] ?? null;
          const item = reading.find((row) => row.evidenceId === payload.evidence_id);
          if (item && state && item.read === null) {
            item.read = state;
            readCount += 1;
          }
          if (current.status !== 'done') {
            const total = reading.length;
            setStep(
              'assess',
              'active',
              total > 0 ? `Read ${Math.min(readCount, total)} of ${total}` : `Read ${readCount}`,
            );
          }
        }
        break;
      }
      case 'checks.done': {
        const quotes = countOf(payload.quotes);
        setStep('verify', 'done', `${quotes} quotes checked`, elapsedFor(event, firstAt));
        break;
      }
      case 'verifier.done':
        if (payload.called === true && typeof payload.reason === 'string' && payload.reason) {
          finding = { supported: payload.verdict === 'SUPPORTED', reason: payload.reason };
        }
        setStep('verify', 'done', undefined, elapsedFor(event, firstAt));
        break;
      case 'assurance.completed':
        setStep('verify', 'done', undefined, elapsedFor(event, firstAt));
        break;
      case 'answer.ready':
      case 'answer.completed':
        setStep('answer', 'done', undefined, elapsedFor(event, firstAt));
        break;
      case 'facts.ready':
        facts = factsFrom(event, ticker);
        break;
      case 'run.completed':
        terminal = { status: 'done' };
        break;
      case 'run.failed':
        {
          const failureCode = payload.failure_code;
          const safeMessage = payload.safe_message;
          const message = failedMessage(failureCode, safeMessage);
          terminal = {
            status: 'failed',
            message,
            costCap:
              failureCode === 'BUDGET_EXCEEDED' ||
              (failureCode === 'SYSTEM_ERROR' && /cost ceiling/i.test(String(safeMessage ?? ''))),
            ...(typeof payload.cost_usd === 'number' ? { spent: payload.cost_usd } : {}),
            ...(typeof payload.max_cost_usd === 'number' ? { cap: payload.max_cost_usd } : {}),
          };
        }
        break;
      case 'run.cancelled':
        terminal = { status: 'cancelled' };
        break;
      default:
        break;
    }
    if (steps.filter((step) => step.status === 'done').length > finishedBefore) {
      const eventAt = Date.parse(event.emitted_at);
      if (Number.isFinite(eventAt)) firstAt = eventAt;
    }
  }

  if (terminal?.status === 'done') {
    steps.forEach((step) => {
      if (step.status === 'pending' || step.status === 'active') step.status = 'done';
    });
  } else if (terminal?.status === 'failed') {
    const active =
      steps.find((step) => step.status === 'active') ?? steps.find((step) => step.status === 'pending');
    if (active) active.status = 'failed';
  } else if (terminal === null && !steps.some((step) => step.status === 'active')) {
    // Only the first stage announces itself; the step after the last finished one is the one in progress.
    const next = steps.find((step) => step.status === 'pending');
    if (next && steps.some((step) => step.status === 'done')) next.status = 'active';
  }
  return { steps, facts, terminal, reading, finding, candidate, readCount };
}
