import type { PipelineStep } from '../../mock/demoCases';

interface Props {
  steps: PipelineStep[];
}

export default function ProcessStepper({ steps }: Props) {
  return (
    <section className="wb-process" aria-labelledby="process-title">
      <div className="wb-section-heading">
        <div>
          <span className="wb-card-kicker">Transparent process</span>
          <h2 id="process-title">Attribution workflow</h2>
        </div>
        <span>Evidence-bounded path</span>
      </div>
      <ol className="wb-process-list">
        {steps.map((step, index) => (
          <li key={step.id} className={`wb-process-step is-${step.status}`}>
            <div className="wb-process-index">{index + 1}</div>
            <div>
              <h3>{step.label}</h3>
              <p>{step.note}</p>
              {step.durationMs != null && <span>{step.durationMs} ms</span>}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
