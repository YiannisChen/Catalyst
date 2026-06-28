import type { AttributionResult, CauseRole, EvidenceItem } from '../../mock/demoCases';
import StatusBadge from './StatusBadge';
import type { WorkflowPhase } from './workflow-state';

interface Props {
  result: AttributionResult | null;
  evidence: EvidenceItem[];
  phase: WorkflowPhase;
  errorMessage: string | null;
}

const causeRoleLabels: Record<CauseRole, string> = {
  primary_driver: 'Primary driver',
  secondary_driver: 'Secondary driver',
  supporting_context: 'Supporting context',
  weak_context: 'Weak context',
};

function formatCauseRole(role: CauseRole): string {
  return causeRoleLabels[role];
}

export default function AttributionAnswerCard({ result, evidence, phase, errorMessage }: Props) {
  const evidenceById = new Map(evidence.map((item) => [item.id, item]));

  if (!result) {
    return (
      <section className="wb-card wb-answer-card" aria-labelledby="answer-title">
        <div className="wb-answer-heading">
          <div>
            <span className="wb-card-kicker">Final assessment</span>
            <h2 id="answer-title">Attribution answer</h2>
          </div>
          {phase === 'running' && <span className="wb-running-badge">Running</span>}
        </div>
        <div className={`wb-answer-empty is-${phase}`} role={phase === 'error' ? 'alert' : 'status'}>
          <strong>{phase === 'running' ? 'Analyzing the selected event window' : phase === 'error' ? 'Attribution run failed' : 'No attribution result yet'}</strong>
          <p>{phase === 'running' ? 'Evidence is being retrieved, criticized, routed, and validated.' : phase === 'error' ? errorMessage : 'Run attribution to generate an evidence-bounded assessment.'}</p>
        </div>
      </section>
    );
  }

  return (
    <section className="wb-card wb-answer-card" aria-labelledby="answer-title">
      <div className="wb-answer-heading">
        <div>
          <span className="wb-card-kicker">Final assessment</span>
          <h2 id="answer-title">{result.label}</h2>
        </div>
        <StatusBadge status={result.status} compact />
      </div>

      <div className="wb-answer-summary">
        <h3>{result.headline}</h3>
        <p>{result.summary}</p>
      </div>

      {result.refused ? (
        <div className="wb-refusal" role="status">
          <strong>Attribution refused</strong>
          <p>{result.statusReason}</p>
        </div>
      ) : (
        <div className="wb-causes" aria-label="Attributed causes">
          {result.causes.map((cause) => (
            <article
              key={cause.title}
              className={`wb-cause wb-cause-${cause.role.includes('driver') ? 'action' : 'neutral'}`}
            >
              <div className="wb-cause-topline">
                <strong className={`wb-cause-role wb-cause-role-${cause.role}`}>
                  {formatCauseRole(cause.role)}
                </strong>
                <span className={`wb-direction wb-direction-${cause.direction}`}>{cause.direction}</span>
              </div>
              <h3>{cause.title}</h3>
              <p className="wb-cause-rationale">{cause.rationale}</p>
              <span className={`wb-support-level wb-support-${cause.supportLevel}`}>
                {cause.supportLevel} support
              </span>
              {cause.confidence != null ? (
                <div className="wb-evidence-support">
                  <div className="wb-evidence-support-label">
                    <span>Evidence support</span>
                    <strong>{Math.round(cause.confidence * 100)}%</strong>
                  </div>
                  <div className="wb-cause-meter" aria-hidden="true">
                    <span className="wb-cause-fill" style={{ width: `${cause.confidence * 100}%` }} />
                  </div>
                </div>
              ) : null}
              <div className="wb-citations" aria-label="Evidence citations">
                {cause.evidenceIds.map((id) => (
                  <span key={id} title={evidenceById.get(id)?.title}>{evidenceById.get(id)?.source ?? id}</span>
                ))}
              </div>
            </article>
          ))}
        </div>
      )}

      <div className="wb-assessment-summary">
        <div className={`wb-status-reason wb-assessment-boundary wb-reason-${result.status.toLowerCase()}`}>
          <span>Evidence boundary</span>
          <p>{result.statusReason}</p>
        </div>
        <dl className="wb-answer-metrics wb-assessment-metrics">
          <div><dt>Grounding</dt><dd>{Math.round(result.groundingRate * 100)}%</dd></div>
          <div><dt>Confidence</dt><dd>{Math.round(result.groundingRate * 100)}%</dd></div>
        </dl>
      </div>
    </section>
  );
}
