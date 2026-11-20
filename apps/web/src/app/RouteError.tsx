import type React from 'react';
import { useNavigate, useRouteError } from 'react-router';
import { StateNotice } from '@/components/catalyst';

export default function RouteError(): React.JSX.Element {
  const navigate = useNavigate();
  useRouteError();
  return (
    <main className="flex h-full items-center justify-center p-6">
      <StateNotice
        tone="down"
        title="Something went wrong on this page"
        body="The rest of Catalyst still works. Reload to try again."
        action={{ label: 'Reload', onClick: () => window.location.reload() }}
        secondaryAction={{ label: 'Go to the workbench', onClick: () => navigate('/') }}
      />
    </main>
  );
}
