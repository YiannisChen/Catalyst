import type { EvidenceItem, PipelineStep } from '../../mock/demoCases';

interface Props {
  steps: PipelineStep[];
  evidence: EvidenceItem[];
  referencedIds: Set<string>;
}

const resolvedStatuses = new Set(['complete', 'warning', 'skipped', 'error']);

function formatDuration(durationMs: number): string {
  return durationMs >= 1_000 ? `${(durationMs / 1_000).toFixed(2)}s` : `${durationMs} ms`;
}

export default function DeveloperDetails({ steps, evidence, referencedIds }: Props) {
  const totalDuration = Math.max(0, ...steps.map((step) => step.durationMs ?? 0));
  const resolvedSteps = steps.filter((step) => resolvedStatuses.has(step.status)).length;
  const topEvidenceScore = evidence.reduce(
    (highest, item) => Math.max(highest, item.relevanceScore),
    0,
  );

  return (
    <details className="wb-developer-details">
      <summary>
        <span>Developer details</span>
        <small>Pipeline diagnostics and evidence scores</small>
      </summary>
      <dl className="wb-diagnostics-summary">
        <div><dt>Total runtime</dt><dd>{formatDuration(totalDuration)}</dd></div>
        <div><dt>Stages resolved</dt><dd>{resolvedSteps}/{steps.length}</dd></div>
        <div><dt>Cited evidence</dt><dd>{referencedIds.size}</dd></div>
        <div><dt>Top evidence score</dt><dd>{topEvidenceScore.toFixed(2)}</dd></div>
      </dl>
      <div className="wb-developer-grid">
        <section>
          <h3>Pipeline diagnostics</h3>
          {steps.map((step) => (
            <div className="wb-diagnostic-row" key={step.id}>
              <code>{step.label}</code>
              <span className={`wb-diagnostic-status is-${step.status}`}>{step.status}</span>
              <span>{step.durationMs == null ? '-' : formatDuration(step.durationMs)}</span>
              <small>{step.note}</small>
            </div>
          ))}
        </section>
        <section>
          <h3>Evidence scores</h3>
          {evidence.map((item) => (
            <div className="wb-evidence-score-row" key={item.id}>
              <div className="wb-evidence-score-label">
                <code>{item.id}</code>
                <span>{item.quality}</span>
                <strong>{item.relevanceScore.toFixed(2)}</strong>
              </div>
              <div className="wb-evidence-score-bar" aria-hidden="true">
                <span style={{ width: `${item.relevanceScore * 100}%` }} />
              </div>
            </div>
          ))}
        </section>
      </div>
      <details className="wb-raw-diagnostics">
        <summary>Show raw diagnostics</summary>
        <pre>{JSON.stringify({ steps, evidence, referencedIds: [...referencedIds] }, null, 2)}</pre>
      </details>
    </details>
  );
}
