import type React from 'react';
import type { RunStep } from './progress';

const stageNames = {
  accepted: 'Accepted',
  retrieve: 'Retrieved',
  assess: 'Assessing',
  verify: 'Checking',
  answer: 'Writing',
} as const;
function stepStatusClass(status: RunStep['status']): string {
  return status === 'done'
    ? 'is-done'
    : status === 'active'
      ? 'is-active'
      : status === 'failed'
        ? 'is-failed'
        : 'is-pending';
}
export function RunSteps({ steps }: { steps: RunStep[] }): React.JSX.Element {
  return (
    <section className="run-progress" aria-label="Steps">
      {steps.map((step) => (
        <div className={`run-progress-step ${stepStatusClass(step.status)}`} key={step.id}>
          <span className="run-progress-bar" />
          <span className="run-progress-label">
            {step.id === 'retrieve' && step.elapsed ? `Retrieved · ${step.elapsed}` : stageNames[step.id]}
          </span>
        </div>
      ))}
    </section>
  );
}
