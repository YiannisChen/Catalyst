import type { InputHTMLAttributes, ReactNode } from 'react';
import { cn } from '@/lib/cn';
export interface InputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'prefix' | 'suffix'> {
  mono?: boolean;
  prefix?: ReactNode;
  suffix?: ReactNode;
}
export function Input({ className, mono, prefix, suffix, ...props }: InputProps) {
  return (
    <label
      className={cn(
        'control-md flex min-w-0 items-center gap-2 rounded-md border border-line-control bg-bg-200 px-3 text-ink-300 focus-within:border-accent',
        className,
      )}
    >
      {prefix}
      <input
        className={cn(
          'min-w-0 flex-1 border-0 bg-transparent text-body-sm text-ink-100 outline-none placeholder:text-ink-300',
          mono && 'font-mono uppercase placeholder:normal-case placeholder:font-sans',
        )}
        {...props}
      />
      {suffix}
    </label>
  );
}
