import type { SelectHTMLAttributes } from 'react';
import { ChevronDown } from 'lucide-react';
import { cn } from '@/lib/cn';

// Native <select> (keyboard and screen-reader behaviour for free) styled like Input.
export interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  mono?: boolean;
}
export function Select({ className, mono, children, ...props }: SelectProps) {
  return (
    <span className={cn('relative flex min-w-0 items-center', className)}>
      <select
        className={cn(
          'control-md w-full min-w-0 appearance-none rounded-md border border-line-control bg-bg-200 pr-6 pl-3 text-body-sm text-ink-100 outline-none focus:border-accent disabled:text-ink-300',
          mono && 'font-mono',
        )}
        {...props}
      >
        {children}
      </select>
      <ChevronDown
        aria-hidden="true"
        size={16}
        strokeWidth={1.5}
        className="pointer-events-none absolute right-2 text-ink-300"
      />
    </span>
  );
}
