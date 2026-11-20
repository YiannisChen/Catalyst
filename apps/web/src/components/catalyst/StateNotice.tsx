import type React from 'react';
import { Link } from 'react-router';
import { Button } from '@/components/ui';

type NoticeAction = { label: string } & (
  { onClick: () => void; to?: never } | { to: string; onClick?: never }
);
export type StateNoticeProps = {
  title: string;
  body: string;
  action?: NoticeAction;
  secondaryAction?: NoticeAction;
  tone?: 'neutral' | 'warn' | 'down';
};

function Action({ action, primary }: { action: NoticeAction; primary: boolean }): React.JSX.Element {
  const className = `inline-flex items-center justify-center rounded-md border px-3 text-body-sm font-medium no-underline ${primary ? 'border-line-control bg-bg-200 text-ink-100 hover:bg-bg-300' : 'border-transparent bg-transparent text-ink-200 hover:bg-bg-300'}`;
  return action.to ? (
    <Link className={`control-sm ${className}`} to={action.to}>
      {action.label}
    </Link>
  ) : (
    <Button size="sm" variant={primary ? 'secondary' : 'ghost'} onClick={action.onClick}>
      {action.label}
    </Button>
  );
}

export function StateNotice({
  title,
  body,
  action,
  secondaryAction,
  tone = 'neutral',
}: StateNoticeProps): React.JSX.Element {
  const marker = tone === 'warn' ? 'bg-warn' : tone === 'down' ? 'bg-down' : 'bg-ink-300';
  return (
    <section role="status" className="flex flex-col gap-3 rounded-md border border-line-100 bg-bg-100 p-4">
      <div className="flex gap-3">
        <span aria-hidden="true" className={`mt-2 size-2 shrink-0 rounded-full ${marker}`} />
        <div className="flex flex-col gap-1">
          <h2 className="type-heading m-0">{title}</h2>
          <p className="type-body-sm m-0 text-ink-200">{body}</p>
        </div>
      </div>
      {(action || secondaryAction) && (
        <div className="flex flex-wrap gap-2 pl-5">
          {action && <Action action={action} primary />}
          {secondaryAction && <Action action={secondaryAction} primary={false} />}
        </div>
      )}
    </section>
  );
}
