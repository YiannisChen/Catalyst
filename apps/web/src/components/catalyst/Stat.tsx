import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';
import { Delta } from './Delta';
export interface StatProps {
  label: string;
  value: ReactNode;
  delta?: number;
  hint?: string;
  size?: 'sm' | 'md';
  className?: string;
}
export function Stat({ label, value, delta, hint, size = 'md', className }: StatProps) {
  return (
    <div className={cn('flex min-w-0 flex-col gap-1', className)}>
      <span className="type-label text-ink-300">{label}</span>
      <span
        className={cn(
          'flex items-baseline gap-2 font-numeric tabular-nums',
          size === 'sm' ? 'text-num' : 'text-num-lg',
        )}
      >
        {value}
        {delta != null && <Delta value={delta} />}
      </span>
      {hint && <span className="type-caption text-ink-200">{hint}</span>}
    </div>
  );
}
