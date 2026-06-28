import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

function readSource(fileName: string): string {
  return readFileSync(new URL(fileName, import.meta.url), 'utf8');
}

const headerSource = readSource('./v4/WorkbenchHeader.tsx');
const sessionSource = readSource('./v4/SessionOverviewPanel.tsx');
const intakeSource = readSource('./v4/EvidenceIntakePanel.tsx');
const summarySource = readSource('./v4/SummaryTab.tsx');
const traceSource = readSource('./v4/TraceTab.tsx');
const evidenceSource = readSource('./v4/EvidenceTab.tsx');
const diagnosticsSource = readSource('./v4/DiagnosticsTab.tsx');
const tabsSource = readSource('./v4/ResultWorkspaceTabs.tsx');
const workbenchSource = readSource('./DemoWorkbench.tsx');
const demoCasesSource = readSource('../../mock/demoCases.ts');

// ── Header (A3: "Catalyst" wordmark only; no Workbench/Ticker/Session visible labels) ──
test('v4 header shows Catalyst and Run Attribution, no Workbench/Ticker/Session labels', () => {
  assert.doesNotMatch(headerSource, /Scenario/);
  assert.doesNotMatch(headerSource, /scenarioName/);
  assert.doesNotMatch(headerSource, /Demo data/);
  assert.match(headerSource, /Catalyst/);
  assert.match(headerSource, /Run Attribution/);
  // A3 delete list — these must NOT appear as visible text
  assert.doesNotMatch(headerSource, />Workbench</);
  assert.doesNotMatch(headerSource, />Ticker</);
  assert.doesNotMatch(headerSource, />Session</);
});

// ── Session Overview (A3: "Session Overview" eyebrow deleted) ──
test('v4 session overview has no Session Overview eyebrow', () => {
  assert.doesNotMatch(sessionSource, /Session Overview/);
  assert.doesNotMatch(sessionSource, /Trade Day Snapshot/);
  // ticker + date are the title now
  assert.match(sessionSource, /demoCase.ticker/);
});

// ── Evidence Intake (A3: "Evidence Intake" deleted; "Candidate news" kept) ──
test('v4 evidence intake uses Candidate news, no Evidence Intake', () => {
  assert.match(intakeSource, /Candidate news/);
  assert.doesNotMatch(intakeSource, /Evidence Intake/);
  assert.doesNotMatch(intakeSource, /Event-window evidence/);
  assert.doesNotMatch(intakeSource, /Candidate evidence/i);
});

// ── Summary: Attributed Drivers, no global Confidence ──
test('v4 summary uses Attributed Drivers and removes global Confidence', () => {
  assert.match(summarySource, /Attribution Summary/);
  assert.match(summarySource, /Attributed Drivers/);
  assert.doesNotMatch(summarySource, /Cause Contributions/);
  assert.doesNotMatch(summarySource, /global confidence/i);
  // Global Confidence metric removed — only Grounding, Accepted, Cited remain
  assert.match(summarySource, /Grounding/);
  assert.match(summarySource, /Accepted evidence/);
  assert.match(summarySource, /Cited evidence/);
  // Explicit Confidence label for each driver
  assert.match(summarySource, /Confidence/);
  // No supportLevel / role / supportWeight
  assert.doesNotMatch(summarySource, /supportWeight/);
  assert.doesNotMatch(summarySource, /supportLevel/);
});

// ── Evidence filters: Accepted/Rejected/Ungraded, no High/Medium/Low ──
test('v4 evidence filters use Accepted/Rejected/Ungraded not High/Medium/Low', () => {
  assert.match(evidenceSource, /Accepted/);
  assert.match(evidenceSource, /Rejected/);
  assert.match(evidenceSource, /Ungraded/);
  assert.doesNotMatch(evidenceSource, /High quality/);
  assert.doesNotMatch(evidenceSource, /Medium/);
  assert.doesNotMatch(evidenceSource, /Low/);
  // criticDecision drives filtering
  assert.match(evidenceSource, /criticDecision/);
  assert.match(evidenceSource, /aria-pressed/);
});

// ── Trace: criticDecision-based grading ──
// ── Trace: criticDecision-based grading + raw output ──
test('v4 trace uses criticDecision and has raw output support', () => {
  assert.match(traceSource, /criticDecision/);
  assert.match(traceSource, /Raw Output/);
  assert.match(traceSource, /v4-raw-toggle/);
  assert.match(traceSource, /redactPayload/);
});

// ── Diagnostics: Decision column, Elapsed time ──
test('v4 diagnostics uses Decision column and accurate labels', () => {
  assert.match(diagnosticsSource, /Decision/);
  assert.match(diagnosticsSource, /Elapsed time/);
  assert.match(diagnosticsSource, /Top relevance/);
  assert.doesNotMatch(diagnosticsSource, /Top evidence score/);
});

// ── Result Workspace (A3: "Analysis" eyebrow deleted; "Attribution Workspace" kept) ──
test('v4 result workspace has Attribution Workspace, no Analysis eyebrow', () => {
  assert.doesNotMatch(tabsSource, /Analysis/);
  assert.match(tabsSource, /Attribution Workspace/);
  assert.match(tabsSource, /role="tablist"/);
  assert.match(tabsSource, /role="tab"/);
  assert.match(tabsSource, /role="tabpanel"/);
  assert.match(tabsSource, /aria-selected/);
  assert.match(tabsSource, /aria-controls/);
  assert.match(tabsSource, /ArrowRight/);
  assert.match(tabsSource, /ArrowLeft/);
});

// ── No old demo/scenario wording reintroduced ──
test('v4 components contain no deprecated labels', () => {
  const v4Files = [headerSource, sessionSource, intakeSource, summarySource, traceSource, evidenceSource, diagnosticsSource, tabsSource, workbenchSource];
  const deprecated = ['Trade Day Snapshot', 'Final assessment', 'Attribution answer', 'Event-window evidence', 'Candidate evidence', 'Intermediate outputs', 'Sufficiency gate', 'Judge Output', 'Pipeline diagnostics', 'Cause Contributions', 'Demo data'];
  for (const src of v4Files) {
    for (const label of deprecated) {
      assert.doesNotMatch(src, new RegExp(label, 'i'), `Found deprecated "${label}"`);
    }
  }
});

// ── Data model: criticDecision exists, supportWeight removed ──
test('mock data model has criticDecision and confidence, not supportWeight', () => {
  assert.match(demoCasesSource, /criticDecision/);
  assert.match(demoCasesSource, /EvidenceDecision/);
  assert.doesNotMatch(demoCasesSource, /supportWeight/);
  assert.match(demoCasesSource, /confidence: number/);
  // AttributionResult no longer has global confidence
  assert.doesNotMatch(demoCasesSource, /confidence: number;\s*\n\s*refused/);
});

// ── No emoji ──
test('v4 components contain no emoji', () => {
  const allSources = [headerSource, sessionSource, intakeSource, summarySource, traceSource, evidenceSource, diagnosticsSource, tabsSource, workbenchSource];
  const emojis = ['\uD83D\uDD0D', '\uD83D\uDCCA', '\u26A1', '\u26A0\uFE0F', '\u2705'];
  for (const src of allSources) {
    for (const emoji of emojis) {
      assert.doesNotMatch(src, new RegExp(emoji));
    }
  }
});
