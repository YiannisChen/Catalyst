// View models for a finished run, derived from GET /api/live-runs/{run_id}/view by src/lib/answer.ts.
// Components render these and never read the raw RunViewDTO.

export type OutcomeTone = 'up' | 'warn' | 'down' | 'neutral' | 'info';

export type AnswerOutcome = {
  label: string; // "Confirmed", "Reported", "With its group", "No clear cause", "Ordinary move", "Failed", "Stopped", "Partial answer"
  tone: OutcomeTone;
};

export type AnswerQuote = {
  id: string; // unique per quote (one evidence passage can back several quotes)
  newsId: string | null; // news item id this quote came from ("web:<hash>"), for linking to the news panel
  role: 'Primary' | 'Secondary' | 'Context';
  text: string; // verbatim
  publisher: string;
  time: string; // "Jul 31, 12:02 PM ET"
  url: string | null;
};

export type AnswerLead = {
  text: string; // headline or possible-explanation text, verbatim
  publisher: string;
  time: string; // "Jul 31, 12:02 PM ET"
  url: string | null;
};

export type AnswerAbstention = {
  reason: string; // abstention.reason_text, verbatim
  possibleExplanations: AnswerLead[];
  leads: AnswerLead[];
};

export type RelativeRow = {
  label: string; // "NOW", "Sector XLK", "Peers (4)", "Market"
  value: number; // percent
  kind: 'target' | 'sector' | 'peers' | 'market';
};

export type RunMeta = {
  model: string | null; // analyst model id
  provider: string | null;
  costUsd: number | null;
  costCapUsd: number | null; // max_cost_usd from the run's persisted policy, when recorded
  costHint: string | null; // "upper bound" when cost_method is upper_bound_charged
  durationSeconds: number | null;
  modelCalls: number | null;
  maxTokens: number | null; // own-key runs: the run's token limit
  usedTokens: number | null; // tokens the run used (reported by the provider, or its upper bound)
  tokensUpperBound: boolean; // a provider did not report usage, so the bound was counted
  createdAt: string | null; // ISO
  finishedAt: string | null; // ISO
};

export type AnswerModel = {
  runId: string;
  ticker: string;
  sessionDate: string;
  outcome: AnswerOutcome;
  productTier:
    | 'CONFIRMED'
    | 'REPORTED'
    | 'MOVED_WITH_GROUP'
    | 'LARGER_THAN_GROUP'
    | 'NOTHING_FOUND'
    | 'ORDINARY_MOVE'
    | null;
  reported: {
    quote: string;
    cause: string;
    scope: string | null;
    publisher: string | null;
    publishedAt: string | null;
    url: string | null;
    eventDate: string | null;
  } | null;
  group: {
    relation: string;
    stockPct: number | null;
    peersPct: number | null;
    peersCount: number | null;
    sectorPct: number | null;
    sectorTicker: string | null;
    residualPct: number | null;
  } | null;
  headline: string | null; // verbatim
  mainCause: string | null; // the answer's key sentence (one source sentence), else the primary hypothesis statement
  alsoCause: string | null; // a second contributing sentence, when the answer names one
  filings: { url: string; time: string }[]; // SEC filings accepted inside the information window
  coMovement: string | null; // facts.comove_sentence, verbatim
  quotes: AnswerQuote[];
  relative: RelativeRow[];
  excessVsSector: number | null;
  gaps: string[]; // plain sentences from limitations
  moveSize: string | null; // "Large move" (T3), "Moved with its group" (T2), "Small move" (T1); else null
  moveSizeReason: string | null; // answer.tier_reason, verbatim, for T1 and T2
  abstention: AnswerAbstention | null; // set when no cause was published
  legacyText: string | null; // old runs without a structured answer
  failureMessage: string | null; // plain text for failed or stopped runs
  stoppedAtCostCap: boolean; // failed because the next model call could pass the run's cost cap
  meta: RunMeta;
};

export type ProcessStage = {
  label: 'Retrieve' | 'Assess' | 'Check + verify' | 'Write';
  startSeconds: number;
  durationSeconds: number;
};

export type ProcessSearch = {
  label: string; // plain label for the search task, or "Search {n}" when unknown
  source: string; // "full-text index", "web search", ...
  items: number;
  seconds: number | null;
  failed: boolean;
};

export type EvidenceDecision = {
  id: string; // evidence_id
  newsId: string | null;
  disposition: 'Supports' | 'Contradicts' | 'Weak' | 'Not relevant';
  source: string; // publisher or "Web page", plus a short news title when the news list has it
  note: string; // verbatim analyst note
};

export type ProposedCause = {
  role: 'Primary' | 'Secondary' | 'Context';
  fit: string; // "strong match", "plausible", "weak match"
  quoteCount: number;
  statement: string; // verbatim
};

export type QuoteCheck = {
  text: string; // verbatim
  kept: boolean;
  reason: string | null; // plain text when rejected, e.g. "Rejected: not a complete sentence"
};

export type RuleCheck = {
  label: string; // "Moves with peers", "About NOW", "Inside the window"
  result: 'Passed' | 'Changed the answer' | 'Not run';
  detail: string | null;
};

export type VerifierResult = {
  verdict: string; // "Supported", "Not supported", "Not run"
  reason: string | null; // verbatim
};

export type ProcessModel = {
  totalSeconds: number | null;
  stages: ProcessStage[];
  searches: ProcessSearch[];
  retrievalSummary: string | null; // "8 found · 8 in pack · all full text"
  decisions: EvidenceDecision[];
  decisionSummary: string | null; // "4 support, 2 weak, 2 not relevant"
  causes: ProposedCause[];
  quoteChecks: QuoteCheck[];
  quoteSummary: string | null; // "5 kept, 1 rejected"
  rules: RuleCheck[];
  verifier: VerifierResult | null;
  traceJson: string | null; // pretty-printed trace for "Copy trace JSON"
};
