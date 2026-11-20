import type { components } from '@/api/schema';

type SessionCoverage = components['schemas']['SessionCoverage'];
export type CoverageCell = { date: string; state: 'Full' | 'Thin' | 'Gap' };
export type CoverageRow = { ticker: string; cells: CoverageCell[]; newsDays: number };

export function buildCoverage(coverage: SessionCoverage[], tickers: string[]): CoverageRow[] {
  const ordered = [...coverage].sort((a, b) => a.session_date.localeCompare(b.session_date));
  // Rows are the covered tickers only; the price store also holds peers and ETFs that never get news.
  const allTickers = new Set(tickers.map((ticker) => ticker.toUpperCase()));
  return [...allTickers]
    .sort()
    .map((ticker) => {
      const cells = ordered.map((session) => {
        const prices = new Set(session.tickers_with_prices.map((value) => value.toUpperCase()));
        const news = new Set(session.tickers_with_news.map((value) => value.toUpperCase()));
        return {
          date: session.session_date,
          state: prices.has(ticker) && news.has(ticker) ? 'Full' : prices.has(ticker) ? 'Thin' : 'Gap',
        } as CoverageCell;
      });
      const newsDays = ordered.filter((session) =>
        session.tickers_with_news.some((value) => value.toUpperCase() === ticker),
      ).length;
      return { ticker, cells, newsDays };
    })
    .sort((a, b) => a.newsDays - b.newsDays || a.ticker.localeCompare(b.ticker));
}
