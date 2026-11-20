import { cn } from '@/lib/cn';
export interface SessionTimelineProps {
  openAt: number;
  closeAt: number;
  cutoffAt?: number;
  items?: { at: number; label: string; evidence?: boolean; selected?: boolean; onClick?: () => void }[];
  labels?: [string, string, string];
  legend?: boolean;
  title?: string;
  className?: string;
}
export function SessionTimeline({
  openAt,
  closeAt,
  cutoffAt,
  items = [],
  labels = ['Prev close', 'Open', 'Close'],
  legend = true,
  title,
  className,
}: SessionTimelineProps) {
  const position = (time: number) =>
    Math.max(
      0,
      Math.min(100, time <= openAt ? (time / openAt) * 40 : 40 + ((time - openAt) / (closeAt - openAt)) * 60),
    );
  const cutoff = cutoffAt == null ? undefined : position(cutoffAt);
  const overnightCount = items.filter((item) => item.at < openAt).length;
  const sessionCount = items.length - overnightCount;
  return (
    <div className={cn('flex flex-col gap-2', className)}>
      {title && (
        <div className="flex items-center justify-between gap-3">
          <span className="type-label text-ink-300">{title}</span>
          <div className="flex flex-wrap justify-end gap-3 type-caption text-ink-300">
            <span className="inline-flex items-center gap-1">
              <i className="size-2 rounded-full bg-accent" />
              Used as evidence
            </span>
            <span className="inline-flex items-center gap-1">
              <i className="size-2 rounded-full bg-ink-300" />
              Other headline
            </span>
            <span className="inline-flex items-center gap-1">
              <i className="timeline-legend-cutoff bg-accent" />
              Cutoff
            </span>
          </div>
        </div>
      )}
      {/* Band names sit above the track: hundreds of dots would cover them inside it. */}
      <div className="relative h-4 type-caption text-ink-200">
        <span className="absolute left-0">Overnight · {overnightCount} headlines</span>
        <span className="absolute left-2/5 pl-2">Session · {sessionCount} headlines</span>
      </div>
      <div className="timeline-track relative">
        <span className="timeline-band absolute left-0 w-2/5 rounded-sm bg-bg-200" />
        <span className="timeline-band absolute left-2/5 w-3/5 rounded-sm bg-bg-300" />
        <span className="timeline-tick absolute left-2/5 w-px bg-line-control" />
        {cutoff != null && (
          <span
            title="Information cutoff"
            className="timeline-cutoff absolute bottom-1 top-1 bg-accent"
            style={{ left: `calc(${cutoff}% - 1px)` }}
          />
        )}
        {items.map((item, index) => (
          <button
            key={`${item.label}-${index}`}
            type="button"
            aria-label={item.label}
            aria-pressed={item.selected}
            title={item.label}
            onClick={item.onClick}
            className={cn(
              'timeline-dot timeline-session-dot absolute -ml-1 rounded-full border-0 bg-ink-300 p-0',
              item.evidence && 'timeline-evidence timeline-evidence-dot bg-accent',
              item.selected && 'bg-accent ring-2 ring-accent-soft',
            )}
            style={{ left: `${position(item.at)}%` }}
          />
        ))}
      </div>
      <div className="timeline-labels relative h-4 text-ink-300">
        <span className="absolute left-0">{labels[0]}</span>
        <span className="absolute left-2/5 -translate-x-1/2">{labels[1]}</span>
        <span className="absolute right-0">{labels[2]}</span>
      </div>
      {legend && !title && (
        <div className="flex flex-wrap gap-3 text-caption text-ink-300">
          <span className="inline-flex items-center gap-1">
            <i className="size-2 rounded-full bg-accent" />
            Used as evidence
          </span>
          <span className="inline-flex items-center gap-1">
            <i className="size-2 rounded-full bg-ink-300" />
            Other news
          </span>
          <span className="inline-flex items-center gap-1">
            <i className="timeline-legend-cutoff bg-accent" />
            Information cutoff
          </span>
        </div>
      )}
    </div>
  );
}
