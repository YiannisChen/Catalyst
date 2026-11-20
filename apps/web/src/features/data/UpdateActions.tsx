import type { JSX } from 'react';
import { useState } from 'react';
import type { components } from '@/api/schema';
import { Button, Select } from '@/components/ui';
import type { ApiError } from '@/features/data/dataPage';
import { fmtTime } from '@/features/data/model';
export function UpdateActions({
  admin,
  running,
  interrupted,
  start,
  startPending,
  startError,
  notifyAvailable,
  nextAllowedAt,
  notify,
  notifyPending,
  notifyError,
  notifiedAt,
}: {
  admin: boolean;
  running: boolean;
  interrupted: boolean;
  start: () => void;
  startPending: boolean;
  startError?: unknown;
  notifyAvailable: boolean;
  nextAllowedAt?: string | null;
  notify: (reason: components['schemas']['NotifyAdminRequest']['reason']) => void;
  notifyPending: boolean;
  notifyError?: unknown;
  notifiedAt?: string;
}): JSX.Element {
  const [confirm, setConfirm] = useState(false);
  const [reason, setReason] = useState<components['schemas']['NotifyAdminRequest']['reason']>('data_stale');
  const code = (startError as ApiError | undefined)?.status;
  if (admin)
    return (
      <div className="flex flex-wrap items-center gap-2">
        {running ? (
          <Button disabled>Update running</Button>
        ) : confirm ? (
          <>
            <span className="type-caption text-ink-200">
              Start an update now? It runs on the server and resumes saved work.
            </span>
            <Button variant="primary" disabled={startPending} onClick={start}>
              {interrupted ? 'Resume update' : 'Confirm'}
            </Button>
            <Button onClick={() => setConfirm(false)}>Cancel</Button>
          </>
        ) : (
          <Button variant="primary" onClick={() => setConfirm(true)}>
            {interrupted ? 'Resume update' : 'Run update now'}
          </Button>
        )}
        {code === 409 && !running && (
          <span className="type-caption text-warn">An update is already running</span>
        )}
        {startError != null && code !== 409 && (
          <span className="type-caption text-down">
            Could not start the update{code ? ` (${code})` : ''}.
          </span>
        )}
      </div>
    );
  if (!notifyAvailable)
    return <span className="type-caption text-ink-300">Updates are run by the admin.</span>;
  if (notifiedAt)
    return (
      <span className="type-caption text-ink-300">
        The admin has been notified. You can ask again after {fmtTime(notifiedAt)} ET.
      </span>
    );
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Select
        aria-label="Reason"
        value={reason}
        onChange={(event) => setReason(event.target.value as typeof reason)}
      >
        <option value="data_stale">Data looks out of date</option>
        <option value="update_failed">The last update failed</option>
        <option value="other">Something else</option>
      </Select>
      <Button
        variant="primary"
        disabled={notifyPending || Boolean(nextAllowedAt && Date.now() < new Date(nextAllowedAt).getTime())}
        onClick={() => notify(reason)}
      >
        Ask the admin to update
      </Button>
      {notifyError != null && <span className="type-caption text-down">Could not send the request.</span>}
    </div>
  );
}
