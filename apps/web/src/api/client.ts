import createClient from 'openapi-fetch';
import type { paths } from './schema';
import { getAdminToken, notifyAdminTokenRejected, removeAdminToken } from '@/lib/storage';

export interface ApiError {
  status: number;
  code: string;
  message: string;
  next_allowed_at?: string;
}
export function normalizeApiError(error: unknown, status: number): ApiError {
  const body = error && typeof error === 'object' ? (error as Record<string, unknown>) : {};
  const detail = typeof body.detail === 'string' ? body.detail : undefined;
  const message =
    typeof body.message === 'string'
      ? body.message
      : (detail ?? (error instanceof Error ? error.message : 'Request failed'));
  const nextAllowedAt = typeof body.next_allowed_at === 'string' ? body.next_allowed_at : undefined;
  return {
    status,
    code: detail ?? String(status),
    message,
    ...(nextAllowedAt ? { next_allowed_at: nextAllowedAt } : {}),
  };
}
export const api = createClient<paths>({ baseUrl: '' });
api.use({
  async onRequest({ request }) {
    const token = getAdminToken();
    if (token) request.headers.set('Authorization', `Bearer ${token}`);
    return request;
  },
  async onError({ error }) {
    throw normalizeApiError(error, 0);
  },
  async onResponse({ response }) {
    if (response.status !== 401 || !getAdminToken()) return response;
    const body = (await response
      .clone()
      .json()
      .catch(() => null)) as { detail?: unknown } | null;
    if (body?.detail === 'admin_unauthenticated') {
      removeAdminToken();
      notifyAdminTokenRejected();
    }
    return response;
  },
});
