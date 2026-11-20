import type React from 'react';
import { useMemo } from 'react';
import { Badge, Button, Panel } from '@/components/ui';
import { RelativeBars } from '@/components/catalyst';
import type { PublicRunEvent } from '@/api/sse';
import { reduceRunEvents, type RunStepStatus } from './progress';

const stageNames = {
  accepted: 'Accepted',
  retrieve: 'Retrieved',
  assess: 'Assessing',
  verify: 'Checking',
  answer: 'Writing',
} as const;

function outcomeLabel(events: PublicRunEvent[]): string | null {
  const completed = [...events].reverse().find((event) => event.event_type === 'run.completed');
  const outcome =
    completed?.payload && typeof completed.payload === 'object'
      ? (completed.payload as Record<string, unknown>).result_status
      : null;
  if (typeof outcome !== 'string') return null;
  if (outcome === 'ANSWERED') return 'Confirmed';
  if (outcome === 'PARTIAL') return 'Reported';
  if (outcome === 'TIER_ONLY') return 'Ordinary move';
  return 'Completed';
}

function stepStatusClass(status: RunStepStatus): string {
  return status === 'done'
    ? 'is-done'
    : status === 'active'
      ? 'is-active'
      : status === 'failed'
        ? 'is-failed'
        : 'is-pending';
}

export function RunningPanelExample({
  events,
  title,
}: {
  events: PublicRunEvent[];
  title: string;
}): React.JSX.Element {
  const progress = useMemo(() => reduceRunEvents(events), [events]);
  return (
    <Panel title={title}>
      {progress.terminal?.status === 'failed' || progress.terminal?.status === 'cancelled' ? (
        <div className="flex flex-col gap-3">
          <p className="m-0 type-body-sm text-ink-200">
            {progress.terminal.status === 'failed' ? progress.terminal.message : 'This run was stopped.'}
          </p>
          <div className="flex gap-2">
            <Button variant="secondary">Try again</Button>
            <Button variant="ghost">Back</Button>
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          {progress.terminal?.status === 'done' ? (
            <Badge tone="info">{outcomeLabel(events) ?? 'Completed'}</Badge>
          ) : (
            <Badge tone="info" dot>
              Running · 16.1 s
            </Badge>
          )}
          <div className="run-progress" aria-label="Steps">
            {progress.steps.map((step) => (
              <div className={`run-progress-step ${stepStatusClass(step.status)}`} key={step.id}>
                <span className="run-progress-bar" />
                <span className="run-progress-label">{stageNames[step.id]}</span>
              </div>
            ))}
          </div>
          {progress.facts && (
            <>
              <h4 className="type-label m-0 text-ink-300">Known so far</h4>
              <p className="m-0 type-body-sm text-ink-200">{progress.facts.headline}</p>
              <RelativeBars
                rows={progress.facts.relative.flatMap((fact, index) =>
                  fact.value === null
                    ? []
                    : [
                        {
                          label: fact.label,
                          value: fact.value,
                          kind: (['target', 'sector', 'peers', 'market'] as const)[index] ?? 'market',
                        },
                      ],
                )}
                excess={
                  progress.facts.relative[0]?.value != null && progress.facts.relative[1]?.value != null
                    ? progress.facts.relative[0].value - progress.facts.relative[1].value
                    : undefined
                }
                excessLabel="Excess vs sector"
              />
            </>
          )}
          <p className="m-0 type-caption text-ink-300">The answer appears once its quotes are checked.</p>
        </div>
      )}
    </Panel>
  );
}
