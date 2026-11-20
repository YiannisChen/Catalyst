import type { JSX } from 'react';
import { fmtDate, scrubError, STEP_DEFS } from '@/features/data/model';
import type { LatestJob, Overview } from '@/features/data/model';
export function Attention({
  overview,
  job,
}: {
  overview: Overview;
  job: LatestJob | undefined;
}): JSX.Element {
  const rows: { title: string; text: string; tone?: string }[] = [];
  const headlineDate = overview.freshness.headlines_through;
  const behind = headlineDate
    ? overview.coverage.filter((s) => s.session_date > headlineDate && !s.tickers_with_news.length).length
    : overview.coverage.length;
  if (behind)
    rows.push({
      title: `Company headlines stop at ${fmtDate(headlineDate)}.`,
      text: 'The headline archive was not refreshed, so recent sessions have no headlines to read.',
    });
  for (const [host, counts] of Object.entries(job?.deferred_by_host ?? {}))
    if (counts.PENDING)
      rows.push({
        title: `${counts.PENDING} article page${counts.PENDING === 1 ? '' : 's'} waiting.`,
        text: `${host} did not answer; it is retried at the end of every run.`,
      });
  for (const failure of job?.failures ?? [])
    rows.push({
      title: `${STEP_DEFS.find((s) => s.key === failure.stage)?.name ?? failure.stage} · ${failure.item} failed.`,
      text: `${scrubError(failure.error)} Retried on the next run.`,
      tone: 'text-ink-300',
    });
  return (
    <div className="flex flex-col gap-2">
      <span className="type-label text-ink-300">Needs attention · {rows.length}</span>
      {rows.length ? (
        rows.map((row, i) => (
          <div key={`${row.title}-${i}`} className="flex items-start gap-2 text-body-sm">
            <span className={`mt-2 h-2 w-2 shrink-0 rounded-full ${row.tone ? 'bg-ink-300' : 'bg-warn'}`} />
            <span>
              <strong className="font-semibold">{row.title}</strong>{' '}
              <span className="text-ink-200">{row.text}</span>
            </span>
          </div>
        ))
      ) : (
        <p className="type-body-sm m-0 text-ink-300">No outstanding issues reported.</p>
      )}
    </div>
  );
}
