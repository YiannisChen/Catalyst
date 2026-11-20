import type { HTMLAttributes } from 'react';
import { cn } from '@/lib/cn';
import { formatPct } from '@/lib/format';
import { ArrowDown, ArrowUp, Minus } from 'lucide-react';
export interface DeltaProps extends HTMLAttributes<HTMLSpanElement> {
  value: number;
  digits?: number;
  chip?: boolean;
  arrow?: boolean;
  text?: string;
}
export function Delta({ value, digits = 2, chip, arrow = true, text, className, ...props }: DeltaProps) {
  const direction = value > 0 ? 'up' : value < 0 ? 'down' : 'flat';
  const label = text ?? formatPct(value, digits);
  const Icon = direction === 'up' ? ArrowUp : direction === 'down' ? ArrowDown : Minus;
  return (
    <span
      className={cn(
        'delta-gap inline-flex items-center whitespace-nowrap font-numeric',
        direction === 'up' ? 'text-up' : direction === 'down' ? 'text-down' : 'text-ink-200',
        chip &&
          (direction === 'up'
            ? 'delta-chip-padding rounded-sm bg-up-soft'
            : direction === 'down'
              ? 'delta-chip-padding rounded-sm bg-down-soft'
              : 'delta-chip-padding rounded-sm bg-bg-300'),
        className,
      )}
      title={label}
      {...props}
    >
      {arrow && <Icon aria-hidden="true" size={16} strokeWidth={1.5} className="delta-arrow" />}
      <span className="sr-only">{direction === 'up' ? 'up ' : direction === 'down' ? 'down ' : 'flat '}</span>
      {label}
    </span>
  );
}
