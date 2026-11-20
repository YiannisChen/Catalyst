import type { HTMLAttributes, ReactNode } from 'react';
import { cn } from '@/lib/cn';
export interface PanelProps extends Omit<HTMLAttributes<HTMLElement>, 'title'> {
  title?: ReactNode;
  actions?: ReactNode[];
  flush?: boolean;
  bodyClassName?: string;
  children?: ReactNode;
}
export function Panel({ title, actions, flush, bodyClassName, children, className, ...props }: PanelProps) {
  return (
    <section
      className={cn(
        'flex h-full min-h-0 min-w-0 flex-col overflow-hidden rounded-lg border border-line-100 bg-bg-100',
        className,
      )}
      {...props}
    >
      {(title || actions) && (
        <header className="panel-header flex shrink-0 items-center justify-between gap-2 border-b border-line-100 px-4">
          <div className="type-heading flex min-w-0 items-center gap-2">{title}</div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={cn('flex min-h-0 flex-1 flex-col gap-4 overflow-auto', !flush && 'p-4', bodyClassName)}>
        {children}
      </div>
    </section>
  );
}
