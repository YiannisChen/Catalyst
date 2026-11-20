import type { HTMLAttributes, ReactNode } from 'react';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/lib/cn';
const badgeVariants = cva(
  'badge-size badge-padding inline-flex w-fit shrink-0 items-center gap-1 rounded-sm text-caption font-semibold tracking-wide whitespace-nowrap',
  {
    variants: {
      tone: {
        neutral: 'bg-bg-300 text-ink-200',
        accent: 'bg-accent-soft text-accent',
        up: 'bg-up-soft text-up',
        down: 'bg-down-soft text-down',
        info: 'bg-info-soft text-info',
        warn: 'bg-warn-soft text-warn',
      },
    },
    defaultVariants: { tone: 'neutral' },
  },
);
export interface BadgeProps extends HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {
  dot?: boolean;
  children?: ReactNode;
}
export function Badge({ className, tone, dot, children, ...props }: BadgeProps) {
  return (
    <span className={cn(badgeVariants({ tone }), className)} {...props}>
      {dot && <span aria-hidden="true" className="badge-dot shrink-0 rounded-full bg-current" />}
      {children}
    </span>
  );
}
