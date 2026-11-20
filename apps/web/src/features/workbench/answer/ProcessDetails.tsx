import type React from 'react';
import { Check, ChevronDown, X } from 'lucide-react';
import { Badge } from '@/components/ui';
import type { ProcessModel } from './model';

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
export function ProcessDetails({ process }: { process: ProcessModel }): React.JSX.Element {
  const keptCount = process.quoteChecks.filter((item) => item.kept).length;
  const rejectedCount = process.quoteChecks.length - keptCount;
  return (
    <details className="process-details">
      <summary className="type-caption text-ink-300">
        Searches, proposed causes, and full check details
        <ChevronDown size={14} className="process-chevron" />
      </summary>
      <div className="flex flex-col gap-3 pt-3">
        {process.searches.length > 0 && (
          <div className="flex flex-col gap-1">
            <SectionLabel>Searches</SectionLabel>
            {process.searches.map((search, index) => (
              <div key={`${search.label}-${index}`} className="process-detail-line">
                <span className={search.failed ? 'text-warn' : 'text-ink-200'}>{search.label}</span>
                <span className="text-ink-300">{search.source}</span>
                <span className="font-numeric text-ink-200">{search.items} items</span>
                <span className="font-numeric text-ink-300">
                  {search.seconds == null ? '—' : `${search.seconds.toFixed(2)} s`}
                </span>
              </div>
            ))}
          </div>
        )}
        {process.causes.length > 0 && (
          <div className="flex flex-col gap-2">
            <SectionLabel>Proposed causes</SectionLabel>
            {process.causes.map((cause, index) => (
              <article
                key={`${cause.role}-${index}`}
                className="rounded-md border border-line-100 bg-bg-200 p-3"
              >
                <div className="flex items-center gap-2">
                  <Badge
                    tone={
                      cause.role === 'Primary' ? 'accent' : cause.role === 'Secondary' ? 'info' : 'neutral'
                    }
                  >
                    {cause.role}
                  </Badge>
                  <span className="type-caption text-ink-300">
                    {cause.fit} · {cause.quoteCount} quotes
                  </span>
                </div>
                <p className="m-0 mt-2 type-body-sm text-ink-200">{cause.statement}</p>
              </article>
            ))}
          </div>
        )}
        {process.quoteChecks.length > 0 && (
          <div className="flex flex-col">
            <SectionLabel>Quote checks</SectionLabel>
            {process.quoteChecks.map((check, index) => (
              <div key={`${index}-${check.text}`} className="process-check-detail">
                <span className={check.kept ? 'text-up' : 'text-down'}>
                  {check.kept ? <Check size={14} /> : <X size={14} />}
                </span>
                <div className="min-w-0">
                  <p className="m-0 type-caption text-ink-200">{check.text}</p>
                  {!check.kept && check.reason && (
                    <p className="m-0 type-caption text-down">{check.reason}</p>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
        {process.rules.map((rule) => (
          <div key={`${rule.label}-detail`} className="process-detail-line">
            <span className="text-ink-200">{rule.label}</span>
            <span className={verdictTone(rule.result)}>
              {rule.result}
              {rule.detail ? ` · ${rule.detail}` : ''}
            </span>
          </div>
        ))}
        {process.verifier?.reason && (
          <p className="m-0 type-caption text-ink-200">{process.verifier.reason}</p>
        )}
        {process.traceJson && (
          <details>
            <summary className="cursor-pointer type-caption text-ink-300">Trace JSON</summary>
            <pre className="overflow-auto whitespace-pre-wrap rounded-md bg-bg-100 p-3 type-caption text-ink-200">
              {process.traceJson}
            </pre>
          </details>
        )}
      </div>
    </details>
  );
}
