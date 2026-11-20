import type React from 'react';
import { useState } from 'react';
import { Button, Input } from '@/components/ui';

export type AccessSectionProps = {
  adminTokenSet: boolean;
  adminTokenEnabled: boolean;
  onSaveAdminToken: (value: string) => void;
  onRemoveAdminToken: () => void;
};

export function AccessSection({
  adminTokenSet,
  adminTokenEnabled,
  onSaveAdminToken,
  onRemoveAdminToken,
}: AccessSectionProps): React.JSX.Element {
  const [editing, setEditing] = useState(false);
  const [token, setToken] = useState('');
  const cancel = () => {
    setEditing(false);
    setToken('');
  };

  return (
    <section id="admin" className="settings-card flex flex-col gap-3">
      <div className="grid gap-3 md:grid-cols-2 md:items-center">
        <div className="flex flex-col gap-1">
          <h2 className="type-heading m-0">Admin</h2>
          <span className="text-caption text-ink-300">
            Only the server owner needs this: it unlocks starting data updates on a public server.
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {editing ? (
            <>
              <Input
                type="password"
                autoComplete="new-password"
                value={token}
                onChange={(event) => setToken(event.target.value)}
                aria-label="Admin token"
                className="w-full settings-control"
              />
              <Button
                size="sm"
                variant="secondary"
                disabled={!token}
                onClick={() => {
                  onSaveAdminToken(token);
                  cancel();
                }}
              >
                Save
              </Button>
              <Button size="sm" variant="ghost" onClick={cancel}>
                Cancel
              </Button>
            </>
          ) : !adminTokenEnabled ? null : (
            <>
              {adminTokenSet ? (
                <>
                  <span className="text-caption text-ink-200">Admin token set</span>
                  <Button size="sm" variant="ghost" onClick={onRemoveAdminToken}>
                    Remove
                  </Button>
                </>
              ) : (
                <Button size="sm" variant="secondary" onClick={() => setEditing(true)}>
                  Enter admin token
                </Button>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}
