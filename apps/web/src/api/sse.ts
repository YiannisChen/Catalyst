import { fetchEventSource } from '@microsoft/fetch-event-source';
import { getAdminToken, notifyAdminTokenRejected, removeAdminToken } from '@/lib/storage';
import type { components } from './schema';

export type PublicRunEvent = components['schemas']['PublicRunEvent'];
export async function streamRun(
  runId: string,
  onEvent: (event: PublicRunEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const token = getAdminToken();
  await fetchEventSource(`/api/live-runs/${encodeURIComponent(runId)}/stream`, {
    signal,
    openWhenHidden: true,
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    async onopen(response) {
      if (response.status !== 401 || !token) return;
      const body = (await response
        .clone()
        .json()
        .catch(() => null)) as { detail?: unknown } | null;
      if (body?.detail === 'admin_unauthenticated' && getAdminToken() === token) {
        removeAdminToken();
        notifyAdminTokenRejected();
      }
    },
    onmessage(message) {
      if (!message.data) return;
      const event = JSON.parse(message.data) as PublicRunEvent;
      onEvent({ ...event, event_type: (message.event || event.event_type) as PublicRunEvent['event_type'] });
    },
    onerror(error) {
      throw error;
    },
  });
}
