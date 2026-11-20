import type React from 'react';
import { useState } from 'react';
import { Copy } from 'lucide-react';
import { Button } from '@/components/ui';
import { formatCount, formatEtDateTime, formatSeconds } from '@/lib/format';
import { formatTokens } from '@/lib/run';
import type { AnswerModel } from './model';

export function RunTab({ answer }: { answer: AnswerModel }): React.JSX.Element {
  const [copied, setCopied] = useState(false);
  const meta = answer.meta;
  const rows: [string, React.ReactNode][] = [
    [
      'Run id',
      <span className="flex min-w-0 items-center gap-2 font-numeric">
        <span className="min-w-0 truncate" title={answer.runId}>
          {answer.runId}
        </span>
        <Button
          size="sm"
          variant="ghost"
          icon={<Copy size={16} strokeWidth={1.5} aria-hidden="true" />}
          aria-label="Copy run id"
          onClick={() => {
            void navigator.clipboard.writeText(answer.runId).then(() => {
              setCopied(true);
              window.setTimeout(() => setCopied(false), 2000);
            });
          }}
        >
          {copied ? 'Copied' : ''}
        </Button>
      </span>,
    ],
    ['Ticker', answer.ticker || null],
    ['Session', answer.sessionDate || null],
    ['Model', meta.provider && meta.model ? `${meta.provider} · ${meta.model}` : (meta.model ?? null)],
    [
      'Cost',
      meta.costUsd == null
        ? null
        : meta.costHint
          ? `≤ $${meta.costUsd.toFixed(3)} · upper bound, the provider did not report usage`
          : `$${meta.costUsd.toFixed(3)}`,
    ],
    ['Time', meta.durationSeconds == null ? null : formatSeconds(meta.durationSeconds)],
    ['Model calls', meta.modelCalls == null ? null : <span className="font-numeric">{meta.modelCalls}</span>],
    [
      'Tokens',
      meta.usedTokens == null ? null : (
        <span className="font-numeric">
          {/* The count adds only calls whose usage the provider reported, so with a missing report
              the true total is at least this much. */}
          {meta.tokensUpperBound ? '≥ ' : ''}
          {formatCount(meta.usedTokens)}
          {meta.maxTokens != null && <span className="text-ink-300"> of {formatTokens(meta.maxTokens)}</span>}
        </span>
      ),
    ],
    ['Started', meta.createdAt ? formatEtDateTime(meta.createdAt) : null],
    ['Finished', meta.finishedAt ? formatEtDateTime(meta.finishedAt) : null],
  ];
  return (
    <div className="flex flex-col gap-4">
      <Button
        variant="secondary"
        className="self-start"
        onClick={() => window.dispatchEvent(new Event('catalyst:run-again'))}
      >
        Run again
      </Button>
      <dl className="polish-run-grid m-0 grid gap-3">
        {rows
          .filter(([, value]) => value != null && value !== '')
          .map(([label, value]) => (
            <div key={label} className={`polish-run-definition ${label === 'Run id' ? 'col-span-full' : ''}`}>
              <dt className="type-caption text-ink-300">{label}</dt>
              <dd className="m-0 min-w-0 break-words type-body-sm text-ink-100">{value}</dd>
            </div>
          ))}
      </dl>
    </div>
  );
}
