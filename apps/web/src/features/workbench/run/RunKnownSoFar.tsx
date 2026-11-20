import type React from 'react';
import { Badge } from '@/components/ui';
import { RelativeBars } from '@/components/catalyst';
import type { RunCandidate, RunFacts, RunReadingItem } from './progress';
import { formatReadingTime, ReadingList } from './RunReadingList';

export function RunKnownSoFar({
  facts,
  relativeRows,
  excess,
  candidate,
  reading,
  readCount,
  checked,
}: {
  facts: RunFacts | null;
  relativeRows: { label: string; value: number; kind: 'target' | 'sector' | 'peers' | 'market' }[];
  excess: number | undefined;
  candidate: RunCandidate | null;
  reading: RunReadingItem[];
  readCount: number;
  /** A second model has already reviewed the candidate, so the checking card is gone. */
  checked: boolean;
}): React.JSX.Element {
  return (
    <section className="run-known flex flex-col gap-2" aria-label="Known so far">
      <h3 className="type-label m-0 text-ink-300">Known so far</h3>
      {facts ? (
        <>
          {excess != null && (
            <p className={`run-excess font-numeric m-0 ${excess >= 0 ? 'text-up' : 'text-down'}`}>
              {excess > 0 ? '+' : excess < 0 ? '−' : ''}
              {Math.abs(excess).toFixed(2)}%<span className="type-body-sm text-ink-200"> vs its sector</span>
            </p>
          )}
          {facts.headline && <p className="m-0 type-body-sm text-ink-200">{facts.headline}</p>}
          {relativeRows.length > 0 && <RelativeBars rows={relativeRows} />}
        </>
      ) : (
        <p className="m-0 type-caption text-ink-300">Facts will appear as they are ready.</p>
      )}
      {candidate && !checked && (
        <div className="run-candidate-card flex flex-col gap-2 rounded-md border border-line-100 bg-bg-200 p-3">
          <Badge tone="accent">Possible cause · checking</Badge>
          <blockquote className="m-0 type-body-sm text-ink-100">{candidate.quote}</blockquote>
          {candidate.publisher && (
            <span className="type-caption text-ink-300">
              {candidate.url ? (
                <a
                  className="text-info hover:underline"
                  href={candidate.url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {candidate.publisher}
                </a>
              ) : (
                candidate.publisher
              )}
              {candidate.publishedAt && ` · ${formatReadingTime(candidate.publishedAt)}`}
            </span>
          )}
        </div>
      )}
      {reading.length > 0 && <ReadingList items={reading} readCount={readCount} />}
    </section>
  );
}
