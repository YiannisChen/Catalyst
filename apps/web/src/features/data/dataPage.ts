import type { components } from '@/api/schema';
import type { LatestJob } from '@/features/data/model';
import { stageCounts, stageDone, stageTotal } from '@/features/data/model';

export type ApiError = { status?: number; message?: string; next_allowed_at?: string };
export const time = (value?: string | null, options: Intl.DateTimeFormatOptions = {}) =>
  value
    ? new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', ...options }).format(new Date(value))
    : '—';
export function errorText(error: unknown): string {
  return (error as ApiError | undefined)?.message ?? 'Could not load data right now.';
}
export function isForbidden(error: unknown): boolean {
  return [401, 403].includes((error as ApiError | undefined)?.status ?? 0);
}
export function pct(value: number, total: number): number {
  return total ? Math.min(100, Math.max(0, (value / total) * 100)) : 0;
}
export function stageWork(job: LatestJob | undefined, key: string): string {
  const count = stageTotal(job, key);
  const done = stageDone(stageCounts(job, key));
  const moverPct = job?.plan_summary?.mover_threshold_pct ?? 2;
  if (key === 'prices') return `${count || job?.plan_summary?.price_symbols || 0} symbols`;
  if (key === 'sec')
    return `${Math.max(0, count - done)} of ${count || job?.plan_summary?.universe_size || 0} left`;
  if (key === 'movers') return `${count} sessions`;
  if (key === 'news')
    return `${job?.stages.news ? count : `≈${count || job?.plan_summary?.items_to_do.news || 0}`} stories`;
  if (key === 'verify') return `${count || job?.plan_summary?.items_to_do.verify || 0} sessions`;
  return `${count || job?.plan_summary?.items_to_do.deferred || 0} pages`;
}
