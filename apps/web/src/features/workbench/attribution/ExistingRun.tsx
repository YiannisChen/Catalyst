import type React from 'react';
import { Button } from '@/components/ui';
import type { components } from '@/api/schema';
import { formatEtDateTime, formatSeconds, formatUsd } from '@/lib/format';
import { historyOutcome } from '@/features/history/outcome';
import { costLabel, costTitle } from '@/lib/run';
export type ExistingRunProps = {
  run: components['schemas']['RunHistoryItem'];
  onOpen: () => void;
  onRunAgain: () => void;
  starting: boolean;
  runError: string | null;
};
export function ExistingRun({
  run,
  onOpen,
  onRunAgain,
  starting,
  runError,
}: ExistingRunProps): React.JSX.Element {
  const outcome = historyOutcome(run);
  // A failed or stopped run has no answer worth opening first; running again is the next step.
  const rerunFirst = run.lifecycle_status === 'FAILED' || run.lifecycle_status === 'CANCELLED';
  const open = (
    <Button key="open" variant={rerunFirst ? 'secondary' : 'primary'} onClick={onOpen}>
      {rerunFirst ? 'Open details' : 'Open answer'}
    </Button>
  );
  const again = (
    <Button
      key="again"
      variant={rerunFirst ? 'primary' : 'ghost'}
      onClick={onRunAgain}
      disabled={starting}
      title="Runs this session again with the model and cost cap in Settings"
    >
      {starting ? 'Starting…' : 'Run again'}
    </Button>
  );
  return (
    <>
      <div>
        <h2 className="type-title m-0">{outcome.label}</h2>
        {run.headline && <p className="type-body-sm text-ink-200">{run.headline}</p>}
      </div>
      <dl className="grid definition-grid gap-2 type-body-sm">
        <dt className="text-ink-300">Model</dt>
        <dd>{run.model ?? '—'}</dd>
        <dt className="text-ink-300">Cost</dt>
        <dd className="font-numeric" title={costTitle(run.cost_method)}>
          {costLabel(run.cost_usd, run.cost_method, 3)}
        </dd>
        <dt className="text-ink-300">Duration</dt>
        <dd className="font-numeric">{formatSeconds(run.duration_ms / 1000)}</dd>
        <dt className="text-ink-300">Created</dt>
        <dd className="font-numeric">{formatEtDateTime(run.created_at)}</dd>
      </dl>
      {rerunFirst ? [again, open] : [open, again]}
      {runError && <p className="m-0 type-caption text-down">{runError}</p>}
    </>
  );
}
