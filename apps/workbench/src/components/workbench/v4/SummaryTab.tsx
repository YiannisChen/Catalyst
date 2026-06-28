import type { AttributionResult } from '../../../mock/demoCases';
import type { WorkflowPhase } from '../workflow-state';
import StatusBadge from '../StatusBadge';

interface Props {
  result: AttributionResult | null;
  phase: WorkflowPhase;
  errorMessage: string | null;
  acceptedCount?: number;
  citedCount?: number;
}

export default function SummaryTab({
  result,
  phase,
  errorMessage,
  acceptedCount = 0,
  citedCount = 0,
}: Props) {
  if (!result) {
    return (
      <div className="v4-summary-empty">
        {phase === 'running' ? (
          <>
            <span className="v4-status-pulse" />
            <strong>Analyzing the selected event window</strong>
            <p>Evidence is being retrieved, reviewed, and validated.</p>
          </>
        ) : phase === 'error' ? (
          <>
            <strong>Attribution run failed</strong>
            <p>{errorMessage || 'An unexpected error occurred.'}</p>
          </>
        ) : (
          <>
            <strong>No attribution result yet</strong>
            <p>Run attribution to generate an evidence-bounded assessment.</p>
          </>
        )}
      </div>
    );
  }

  const hasDrivers = !result.refused && result.causes.length > 0;

  return (
    <div className="v4-summary">
      {/* Top bar: label + status badge */}
      <div className="v4-summary-head">
        <div>
          <span className="v4-kicker">Attribution Summary</span>
          <h3>{result.label}</h3>
        </div>
        <StatusBadge status={result.status} compact />
      </div>

      {/* Paired column headers */}
      <div className="v4-summary-cols-head">
        <h4 className="v4-summary-col-title">Summary</h4>
        {hasDrivers && <h4 className="v4-summary-col-title">Attributed Drivers</h4>}
      </div>

      {/* Two-column grid */}
      <div className={hasDrivers ? 'v4-summary-grid' : 'v4-summary-grid v4-summary-grid-single'}>
        {/* Left: headline, answer, boundary, metrics */}
        <div className="v4-summary-left">
          <div className="v4-summary-card">
            <p className="v4-headline">{result.headline}</p>
            <p className="v4-summary-text">{result.summary}</p>
          </div>

          {result.refused && (
            <div className="v4-refusal">
              <strong>Attribution refused</strong>
              <p>{result.statusReason}</p>
            </div>
          )}

          <div className="v4-summary-card">
            <div className={`v4-boundary-segment v4-boundary-${result.status.toLowerCase()}`}>
              <span className="v4-boundary-label">Evidence Boundary</span>
              <p>{result.statusReason}</p>
            </div>
          </div>

          <dl className="v4-score-grid">
            <div>
              <dt>Grounding</dt>
              <dd>{Math.round(result.groundingRate * 100)}%</dd>
            </div>
            <div>
              <dt>Accepted evidence</dt>
              <dd>{acceptedCount}</dd>
            </div>
            <div>
              <dt>Cited evidence</dt>
              <dd>{citedCount}</dd>
            </div>
          </dl>
        </div>

        {/* Right: attributed drivers */}
        {hasDrivers && (
          <div className="v4-summary-right">
            <div className="v4-causes-compact">
              {result.causes.map((cause) => (
                <div key={cause.title} className="v4-cause-compact">
                  <div className="v4-cause-compact-head">
                    <h4 className="v4-cause-title">{cause.title}</h4>
                    <div className="v4-cause-compact-badges">
                      <span className={`v4-direction-badge v4-dir-${cause.direction}`}>
                        {cause.direction}
                      </span>
                    </div>
                  </div>
                  <div className="v4-cause-compact-meta">
                    <span className="v4-confidence-label">
                      Confidence {Math.round(cause.confidence * 100)}%
                    </span>
                  </div>
                  <p className="v4-cause-rationale">{cause.rationale}</p>
                  <div className="v4-citations">
                    {cause.evidenceIds.map((id) => (
                      <code key={id} className="v4-citation" title={id}>{id}</code>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
