import { fetchEventSource } from '@microsoft/fetch-event-source';
import { getAdminToken, notifyAdminTokenRejected, removeAdminToken } from '@/lib/storage';
import type { components } from './schema';

export type PublicRunEvent = components['schemas']['PublicRunEvent'];
export async function streamRun(
  runId: string,
  onEvent: (event: PublicRunEvent) => void,
  signal: AbortSignal,
  onRetry: () => void,
): Promise<void> {
  const token = getAdminToken();
  let retryAttempt = 0;
  let receivedTerminalEvent = false;
  class NonRetryableStreamError extends Error {}

  await fetchEventSource(`/api/live-runs/${encodeURIComponent(runId)}/stream`, {
    signal,
    openWhenHidden: true,
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    async onopen(response) {
      if (response.status >= 400 && response.status < 500) {
        if (response.status === 401 && token) {
          const body = (await response
            .clone()
            .json()
            .catch(() => null)) as { detail?: unknown } | null;
          if (body?.detail === 'admin_unauthenticated' && getAdminToken() === token) {
            removeAdminToken();
            notifyAdminTokenRejected();
          }
        }
        throw new NonRetryableStreamError(`Run stream rejected with status ${response.status}.`);
      }
      retryAttempt = 0;
    },
    onmessage(message) {
      if (!message.data) return;
      const event = JSON.parse(message.data) as PublicRunEvent;
      const publicEvent = {
        ...event,
        event_type: (message.event || event.event_type) as PublicRunEvent['event_type'],
      };
      if (['run.completed', 'run.failed', 'run.cancelled'].includes(publicEvent.event_type)) {
        receivedTerminalEvent = true;
      }
      onEvent(publicEvent);
    },
    onclose() {
      if (!receivedTerminalEvent) throw new Error('Run stream closed before a terminal event.');
    },
    onerror(error) {
      if (error instanceof NonRetryableStreamError) throw error;
      if (retryAttempt >= 8) throw error;
      onRetry();
      const delay = Math.min(1000 * 2 ** retryAttempt, 15_000);
      retryAttempt += 1;
      return delay;
    },
  });
}
