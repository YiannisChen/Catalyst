import { cn } from '@/lib/cn';
import { Delta } from './Delta';
export interface RelativeBarsProps {
  rows: { label: string; kind: 'target' | 'market' | 'sector' | 'peers'; value: number }[];
  excess?: number;
  excessLabel?: string;
  max?: number;
  className?: string;
}
const colors = {
  target: 'bg-accent',
  market: 'bg-data-market',
  sector: 'bg-data-sector',
  peers: 'bg-data-peers',
};
export function RelativeBars({
  rows,
  excess,
  excessLabel = 'Excess vs sector',
  max,
  className,
}: RelativeBarsProps) {
  const scale = max || Math.max(1, ...rows.map(({ value }) => Math.abs(value)));
  return (
    <div className={cn('flex flex-col gap-2', className)}>
      {rows.map((row) => {
        const width = Math.min(50, (Math.abs(row.value) / scale) * 50);
        return (
          <div key={row.label} className="relative-row items-center gap-3">
            <span className="flex min-w-0 items-center gap-2 truncate text-caption text-ink-200">
              <i className={cn('size-2 rounded-full', colors[row.kind])} />
              {row.label}
            </span>
            <span className="relative h-4">
              <i className="absolute bottom-0 top-0 left-1/2 w-px bg-line-200" />
              <i
                className={cn('relative-bar absolute rounded-sm', colors[row.kind])}
                style={{ left: `${row.value >= 0 ? 50 : 50 - width}%`, width: `${width}%` }}
              />
            </span>
            <Delta value={row.value} arrow={false} className="justify-end text-right text-caption" />
          </div>
        );
      })}
      {excess != null && (
        <div className="flex justify-between gap-2 border-t border-line-100 pt-2 text-caption text-ink-200">
          <span>{excessLabel}</span>
          <Delta value={excess} />
        </div>
      )}
    </div>
  );
}
