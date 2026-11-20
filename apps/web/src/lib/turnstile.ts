// Cloudflare Turnstile for anonymous runs on the public site. The API requires a fresh token
// (X-Turnstile-Token) on every anonymous POST /api/live-runs; admins and invited users skip it.
// The site key is public and injected at build time; without it (local use) nothing loads.

type TurnstileApi = {
  render: (container: HTMLElement, options: Record<string, unknown>) => string;
  remove: (widgetId: string) => void;
};

declare global {
  interface Window {
    turnstile?: TurnstileApi;
  }
}

const SITE_KEY = import.meta.env.VITE_TURNSTILE_SITE_KEY?.trim() ?? '';
const SCRIPT_URL = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
const TIMEOUT_MS = 30_000;

export const turnstileEnabled = SITE_KEY.length > 0;

let script: Promise<TurnstileApi> | null = null;

function loadTurnstile(): Promise<TurnstileApi> {
  if (window.turnstile) return Promise.resolve(window.turnstile);
  script ??= new Promise<TurnstileApi>((resolve, reject) => {
    const tag = document.createElement('script');
    tag.src = SCRIPT_URL;
    tag.async = true;
    tag.onload = () =>
      window.turnstile ? resolve(window.turnstile) : reject(new Error('turnstile_unavailable'));
    tag.onerror = () => {
      script = null;
      reject(new Error('turnstile_unavailable'));
    };
    document.head.appendChild(tag);
  });
  return script;
}

/**
 * A one-time token proving a person started this run. Usually invisible: the widget only shows
 * (bottom right) when Cloudflare wants an interaction. Rejects if it cannot be had in time.
 */
export async function getTurnstileToken(): Promise<string> {
  const api = await loadTurnstile();
  const host = document.createElement('div');
  host.className = 'turnstile-host';
  document.body.appendChild(host);
  let widgetId: string | null = null;
  try {
    return await new Promise<string>((resolve, reject) => {
      const timer = window.setTimeout(() => reject(new Error('turnstile_timeout')), TIMEOUT_MS);
      const fail = (reason: string) => () => {
        window.clearTimeout(timer);
        reject(new Error(reason));
      };
      widgetId = api.render(host, {
        sitekey: SITE_KEY,
        action: 'run',
        appearance: 'interaction-only',
        theme: 'dark',
        callback: (token: string) => {
          window.clearTimeout(timer);
          resolve(token);
        },
        'error-callback': fail('turnstile_error'),
        'expired-callback': fail('turnstile_expired'),
        'timeout-callback': fail('turnstile_timeout'),
      });
    });
  } finally {
    if (widgetId) api.remove(widgetId);
    host.remove();
  }
}
