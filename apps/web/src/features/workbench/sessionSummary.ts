import type { components } from '@/api/schema';

type Session = components['schemas']['SessionResponse'];
type RunHistoryItem = components['schemas']['RunHistoryItem'];

export type SessionSummary = {
  move: number;
  tradingDay: boolean;
  gap: number | null; // open vs previous close, percent
  intraday: number | null; // close vs open, percent
  avgCost: number | null; // from the user's recent runs
  avgSeconds: number | null;
  timedRuns: number;
};

// Numbers shown in the "Ready to run" card, derived from the session and recent runs.
export function summarizeSession(session: Session | undefined, recentRuns: RunHistoryItem[]): SessionSummary {
  const timed = recentRuns.filter((item) => item.cost_usd != null && item.duration_ms != null);
  const average = (values: number[]) =>
    values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
  const avgMs = average(timed.map((item) => item.duration_ms));
  return {
    move: session?.close_move_pct ?? 0,
    tradingDay: session?.is_trading_day !== false,
    gap:
      session?.open != null && session.previous_close
        ? (session.open / session.previous_close - 1) * 100
        : null,
    intraday: session?.close != null && session.open ? (session.close / session.open - 1) * 100 : null,
    avgCost: average(timed.map((item) => item.cost_usd ?? 0)),
    avgSeconds: avgMs === null ? null : avgMs / 1000,
    timedRuns: timed.length,
  };
}
