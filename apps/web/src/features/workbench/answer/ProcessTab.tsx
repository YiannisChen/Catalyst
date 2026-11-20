import type React from 'react';
import { Check, ChevronDown, X } from 'lucide-react';
import { Badge } from '@/components/ui';
import { formatSeconds } from '@/lib/format';
import { StateNotice } from '@/components/catalyst';
import { ProcessDetails } from './ProcessDetails';
import type { EvidenceDecision, ProcessModel } from './model';

function sourceVerdict(decision: EvidenceDecision): string {
  switch (decision.disposition) {
    case 'Supports':
      return 'Supports';
    case 'Contradicts':
      return 'Contradicts';
    case 'Weak':
      return 'Weak';
    default:
      return 'Not relevant';
  }
}

function verdictTone(verdict: string): string {
  return verdict === 'Supports' || verdict === 'Supported' || verdict === 'Passed'
    ? 'text-up'
    : verdict === 'Contradicts' || verdict === 'Not supported'
      ? 'text-down'
      : verdict === 'Changed the answer'
        ? 'text-warn'
        : 'text-ink-300';
}

function SectionLabel({ children }: { children: React.ReactNode }): React.JSX.Element {
  return <h3 className="m-0 type-label text-ink-300">{children}</h3>;
}

export function ProcessTab({
  process,
  onRunAgain,
  featured = false,
}: {
  process: ProcessModel;
  onRunAgain?: () => void;
  featured?: boolean;
}): React.JSX.Element {
  const hasData =
    process.stages.length +
      process.searches.length +
      process.decisions.length +
      process.causes.length +
      process.quoteChecks.length +
      process.rules.length >
      0 || process.verifier != null;
  if (!hasData && featured) {
    return (
      <StateNotice
        title="Steps are not part of featured cases"
        body="Featured cases publish the answer and its sources only. Explain this day yourself to see each step."
      />
    );
  }
  if (!hasData) {
    return (
      <StateNotice
        title="No step record for this run"
        body="This run was made before step records were kept. The answer is still valid; run it again to see each step."
        action={onRunAgain ? { label: 'Run again', onClick: onRunAgain } : undefined}
      />
    );
  }

  const decisive = process.decisions.find((item) => item.disposition === 'Supports') ?? process.decisions[0];
  // The item that decided the answer leads the list; the rest keep their reading order.
  const orderedDecisions = [decisive, ...process.decisions.filter((item) => item !== decisive)].filter(
    (item): item is NonNullable<typeof item> => item != null,
  );
  const visibleRows = orderedDecisions.slice(0, 6);
  const extraCount = Math.max(0, process.decisions.length - visibleRows.length);
  const keptCount = process.quoteChecks.filter((item) => item.kept).length;
  const rejectedCount = process.quoteChecks.length - keptCount;
  const supportedCount = process.decisions.filter((item) => item.disposition === 'Supports').length;
  const verifierVerdict = process.verifier?.verdict;
  const total =
    process.totalSeconds ??
    Math.max(0, ...process.stages.map((stage) => stage.startSeconds + stage.durationSeconds));
  const stageCaption = process.stages
    .map((stage) => `${stage.label} ${formatSeconds(stage.durationSeconds)}`)
    .join(' · ');

  return (
    <div className="process-overview flex flex-col gap-4">
      <p className="m-0 type-label text-ink-300">How the answer was reached</p>
      <section className="flex flex-col gap-3">
        <div className="grid grid-cols-4 gap-2">
          <div className="process-funnel-box">
            <strong className="font-numeric type-heading">{process.searches.length}</strong>
            <span className="type-caption text-ink-300">searches</span>
          </div>
          <div className="process-funnel-box">
            <strong className="font-numeric type-heading">{process.decisions.length}</strong>
            <span className="type-caption text-ink-300">read in full</span>
          </div>
          <div className="process-funnel-box process-funnel-focus">
            <strong className="font-numeric type-heading">{supportedCount}</strong>
            <span className="type-caption">states the cause</span>
          </div>
          <div className="process-funnel-box">
            <strong
              className={`font-numeric type-heading ${verifierVerdict === 'Supported' ? 'text-up' : verifierVerdict === 'Not supported' ? 'text-down' : 'text-ink-300'}`}
            >
              {verifierVerdict === 'Supported' ? '✓' : verifierVerdict === 'Not supported' ? '✗' : '—'}
            </strong>
            <span className="type-caption text-ink-300">
              {verifierVerdict === 'Supported'
                ? 'second model agreed'
                : verifierVerdict === 'Not supported'
                  ? 'second model disagreed'
                  : 'second model'}
            </span>
          </div>
        </div>
        <p className="m-0 type-body-sm text-ink-200">
          {process.searches.length} searches recorded · {process.decisions.length} items assessed
          {verifierVerdict ? ` · second model: ${verifierVerdict}` : ''}.
        </p>
        {decisive && (
          <div className="flex flex-col gap-2 border-t border-line-100 pt-3">
            <SectionLabel>What decided it</SectionLabel>
            <article className="process-decisive-card flex flex-col gap-2 rounded-md bg-bg-300 p-3">
              <span className="type-caption text-ink-300">{decisive.source}</span>
              <p className="m-0 type-body text-ink-100">{decisive.note}</p>
              <span className="type-caption text-ink-200">
                {sourceVerdict(decisive)} · {keptCount} quotes kept, {rejectedCount} rejected
              </span>
            </article>
          </div>
        )}
        {(process.rules.length > 0 || process.verifier || process.quoteChecks.length > 0) && (
          <div className="flex flex-col gap-2 border-t border-line-100 pt-3">
            <SectionLabel>Checks</SectionLabel>
            <div className="grid grid-cols-2 gap-2">
              {process.rules.map((rule) => {
                const passed = rule.result === 'Passed';
                return (
                  <div key={rule.label} className="process-check-chip">
                    <span
                      className={
                        passed
                          ? 'text-up'
                          : rule.result === 'Changed the answer'
                            ? 'text-warn'
                            : 'text-ink-300'
                      }
                    >
                      {passed ? <Check size={14} /> : rule.result === 'Not run' ? '—' : <X size={14} />}
                    </span>
                    <span className="type-caption text-ink-200">{rule.label}</span>
                  </div>
                );
              })}
              {process.verifier && (
                <div className="process-check-chip">
                  <span className={verifierVerdict === 'Supported' ? 'text-up' : 'text-down'}>
                    {verifierVerdict === 'Supported' ? <Check size={14} /> : <X size={14} />}
                  </span>
                  <span className="type-caption text-ink-200">Second model: {verifierVerdict}</span>
                </div>
              )}
              {process.quoteChecks.length > 0 && (
                <div className="process-check-chip">
                  <span className="text-ink-300">
                    <Check size={14} />
                  </span>
                  <span className="type-caption text-ink-200">Quotes checked · {keptCount} kept</span>
                </div>
              )}
            </div>
          </div>
        )}
        <div className="flex flex-col border-t border-line-100 pt-3">
          <div className="flex items-baseline justify-between gap-3 pb-1">
            <SectionLabel>Everything read · {process.decisions.length}</SectionLabel>
          </div>
          <div className="process-read-row process-read-heading" aria-hidden="true">
            <span>Verdict</span>
            <span>Summary</span>
          </div>
          {visibleRows.map((decision, index) => (
            <div
              key={`${decision.id}-${index}`}
              className={`process-read-row ${decision === decisive ? 'process-read-decisive' : ''}`}
            >
              <span className={`type-caption ${verdictTone(sourceVerdict(decision))}`}>
                {sourceVerdict(decision)}
              </span>
              <span className="process-read-summary type-body-sm text-ink-200" title={decision.note}>
                {decision.note}
              </span>
            </div>
          ))}
          {extraCount > 0 && (
            <details className="process-more">
              <summary className="type-caption text-info">
                Show the other {extraCount}
                <ChevronDown size={14} className="process-chevron" />
              </summary>
              {orderedDecisions.slice(6).map((decision, index) => (
                <div key={`${decision.id}-extra-${index}`} className="process-read-row">
                  <span className={`type-caption ${verdictTone(sourceVerdict(decision))}`}>
                    {sourceVerdict(decision)}
                  </span>
                  <span className="process-read-summary type-body-sm text-ink-200" title={decision.note}>
                    {decision.note}
                  </span>
                </div>
              ))}
            </details>
          )}
        </div>
        <ProcessDetails process={process} />
        {process.stages.length > 0 && (
          <p className="m-0 border-t border-line-100 pt-2 type-caption text-ink-300">
            Stages · {stageCaption}
            {process.totalSeconds != null
              ? ` · ${formatSeconds(process.totalSeconds)} total`
              : total > 0
                ? ` · ${formatSeconds(total)} total`
                : ''}
          </p>
        )}
      </section>
    </div>
  );
}
