import { useMemo } from 'react';
import type { EvidenceItem, PipelineStep } from '../../../mock/demoCases';

interface Props {
  steps: PipelineStep[];
  evidence: EvidenceItem[];
  referencedIds: Set<string>;
  runtimeMs?: number;
}

function formatDuration(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${ms}ms`;
}

export default function DiagnosticsTab({ steps, evidence, referencedIds, runtimeMs }: Props) {
  const resolvedCount = useMemo(
    () => steps.filter((s) => ['complete', 'warning', 'skipped', 'error'].includes(s.status)).length,
    [steps],
  );
  const topScore = useMemo(
    () => evidence.reduce((max, e) => Math.max(max, e.relevanceScore), 0),
    [evidence],
  );

  return (
    <div className="v4-diagnostics">
      <div className="v4-diag-summary">
        <div className="v4-diag-card">
          <span className="v4-diag-value">{runtimeMs != null ? formatDuration(runtimeMs) : '—'}</span>
          <span className="v4-diag-label">Elapsed time</span>
        </div>
        <div className="v4-diag-card">
          <span className="v4-diag-value">{resolvedCount}/{steps.length}</span>
          <span className="v4-diag-label">Stages resolved</span>
        </div>
        <div className="v4-diag-card">
          <span className="v4-diag-value">{referencedIds.size}</span>
          <span className="v4-diag-label">Cited evidence</span>
        </div>
        <div className="v4-diag-card">
          <span className="v4-diag-value">{topScore.toFixed(2)}</span>
          <span className="v4-diag-label">Top relevance</span>
        </div>
      </div>

      <details className="v4-diag-collapse">
        <summary>
          <span className="v4-diag-summary-chevron" aria-hidden="true" />
          <span className="v4-diag-summary-label">Execution Diagnostics</span>
          <span className="v4-diag-summary-hint">Click to expand</span>
          <span className="v4-diag-summary-chip">{steps.length} stages</span>
        </summary>
        <div className="v4-diag-table">
          <div className="v4-diag-table-head"><span>Stage</span><span>Status</span><span>Duration</span></div>
          {steps.map((step) => (
            <div key={step.id} className="v4-diag-row">
              <code>{step.label}</code>
              <span className={`v4-diag-status v4-diag-${step.status}`}>{step.status}</span>
              <span>{step.durationMs == null ? '-' : formatDuration(step.durationMs)}</span>
              <small className="v4-diag-note">{step.note}</small>
            </div>
          ))}
        </div>
      </details>

      {evidence.length > 0 && (
        <details className="v4-diag-collapse">
          <summary>
            <span className="v4-diag-summary-chevron" aria-hidden="true" />
            <span className="v4-diag-summary-label">Evidence Scores</span>
            <span className="v4-diag-summary-hint">Click to expand</span>
            <span className="v4-diag-summary-chip">{evidence.length} items</span>
          </summary>
          <div className="v4-diag-table">
            <div className="v4-diag-table-head"><span>ID</span><span>Decision</span><span>Relevance</span><span>Timing</span></div>
            {evidence.map((item) => (
              <div key={item.id} className="v4-diag-row">
                <code>{item.id}</code><span>{item.criticDecision}</span>
                <span>{item.relevanceScore.toFixed(2)}</span><span>{item.temporalStatus}</span>
              </div>
            ))}
          </div>
        </details>
      )}

      <details className="v4-diag-collapse">
        <summary>
          <span className="v4-diag-summary-chevron" aria-hidden="true" />
          <span className="v4-diag-summary-label">Raw Diagnostics</span>
          <span className="v4-diag-summary-hint">Click to expand</span>
          <span className="v4-diag-summary-chip">JSON</span>
        </summary>
        <pre>{JSON.stringify(
          { steps, evidence: evidence.map((e) => ({ id: e.id, decision: e.criticDecision, relevance: e.relevanceScore, temporal: e.temporalStatus })), referencedIds: [...referencedIds] },
          null, 2,
        )}</pre>
      </details>
    </div>
  );
}
