import { fixtures } from '@/mocks/fixtures';
import type { PriceRow, NewsCardProps } from '@/components/catalyst';

type FixturePayload = { response?: Record<string, unknown> };
export const findFixture = (name: string) =>
  fixtures.find(({ path }) => path.endsWith(`${name}.json`))?.fixture as FixturePayload | undefined;
export const nowFixture = findFixture('page-a-answered-now-b5');
export const newsFixture = findFixture('page-a-news-images-pending');
export const runningFixture = findFixture('page-a-running-stage-3');
export const tierFixture = findFixture('page-a-tier-only-aapl-2026-03-24');
export const answer = nowFixture?.response?.answer as
  | {
      facts?: {
        target_return_pct?: number;
        market?: { return_pct?: number };
        sector?: { return_pct?: number };
        peers?: { median_return_pct?: number };
      };
      sources?: { text?: string; publisher?: string; published_at?: string; role?: string; url?: string }[];
      window?: { information_window_start_at?: string; session_open_at?: string; cutoff_at?: string };
    }
  | undefined;
export const facts = answer?.facts;
export const newsItems = (newsFixture?.response?.items ?? []) as {
  title: string;
  summary?: string | null;
  publisher: string;
  published_at: string;
  phase?: 'PRE_OPEN' | 'SESSION';
  category?: NewsCardProps['category'];
  used_as_evidence?: boolean;
  image_url?: string | null;
}[];
export const realNews = newsItems.slice(0, 4);
export const firstNews = realNews[0];
export const coverNews = newsItems.find((item) => item.image_url) ?? firstNews;
export const source = answer?.sources?.[0];
export const headline = (nowFixture?.response?.answer as { headline?: string } | undefined)?.headline;
export const running = runningFixture?.response?.run as { lifecycle?: string } | undefined;
export const tierAnswer = tierFixture?.response?.answer as
  { outcome?: string; headline?: string; session_date?: string } | undefined;
export const hasPendingImages = Number(newsFixture?.response?.images_pending ?? 0) > 0;
export const timelineStart = answer?.window?.information_window_start_at;
export const timelineOpenAt =
  timelineStart && answer?.window?.session_open_at
    ? Math.round(
        (new Date(answer.window.session_open_at).getTime() - new Date(timelineStart).getTime()) / 60000,
      )
    : 810;
export const timelineCloseAt = timelineOpenAt + 390;
export const timelineCutoffAt =
  timelineStart && answer?.window?.cutoff_at
    ? Math.round((new Date(answer.window.cutoff_at).getTime() - new Date(timelineStart).getTime()) / 60000)
    : 840;
export const chartRows: PriceRow[] = Array.from({ length: 120 }, (_, index) => {
  const date = new Date(Date.UTC(2026, 0, 2 + index));
  const day = date.toISOString().slice(0, 10);
  const base = 190 + index * 0.12 + Math.sin(index * 0.31) * 2.8 + Math.cos(index * 0.13) * 1.6;
  const open = base + Math.sin(index * 1.7) * 0.8;
  const close = base + Math.cos(index * 1.1) * 1.1;
  return {
    date: day,
    open,
    close,
    high: Math.max(open, close) + 0.7 + (index % 3) * 0.08,
    low: Math.min(open, close) - 0.6 - (index % 4) * 0.06,
    volume: 25000000 + (index % 17) * 950000,
  };
});
export const chartMarks = [24, 67, 101]
  .map((index) => chartRows[index]?.date)
  .filter((date): date is string => Boolean(date));
