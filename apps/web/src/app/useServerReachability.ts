import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';

// Status 0: the request never got an answer. 502/504: the proxy in front of the API got none.
// The API's own 503 (a data store not configured) is not an outage and stays a panel error.
const unreachableStatuses = new Set([0, 502, 504]);

function isNetworkError(error: unknown): boolean {
  if (!error || typeof error !== 'object' || !('status' in error)) return false;
  return unreachableStatuses.has((error as { status: unknown }).status as number);
}

export function useServerReachability(): { unreachable: boolean; retry: () => void } {
  const client = useQueryClient();
  const [unreachable, setUnreachable] = useState(false);
  useEffect(() => {
    const unsubscribeQueries = client.getQueryCache().subscribe((event) => {
      if (event.type !== 'updated') return;
      if (event.action.type === 'error' && isNetworkError(event.query.state.error)) setUnreachable(true);
      if (event.action.type === 'success') setUnreachable(false);
    });
    const unsubscribeMutations = client.getMutationCache().subscribe((event) => {
      if (
        event.type === 'updated' &&
        event.action.type === 'error' &&
        isNetworkError(event.mutation.state.error)
      )
        setUnreachable(true);
    });
    return () => {
      unsubscribeQueries();
      unsubscribeMutations();
    };
  }, [client]);
  useEffect(() => {
    if (!unreachable) return;
    const timer = window.setInterval(() => void client.refetchQueries({ type: 'active' }), 10_000);
    return () => window.clearInterval(timer);
  }, [client, unreachable]);
  return { unreachable, retry: () => void client.refetchQueries({ type: 'active' }) };
}
