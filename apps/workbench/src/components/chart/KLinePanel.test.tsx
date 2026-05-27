import { delay, http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import App from '../../App';
import { render, screen } from '../../test/render';
import { server } from '../../test/server';

describe('KLinePanel', () => {
  it('loads ohlcv for selected ticker and shows empty state when no candles', async () => {
    const requestedUrls: URL[] = [];

    server.use(
      http.get('/api/tickers', () => HttpResponse.json({ symbols: ['AAPL'], count: 1 })),
      http.get('/api/range-local', () =>
        HttpResponse.json({
          min_date: '2024-01-02',
          max_date: '2024-03-29',
          ticker_count: 1,
          row_count: 0,
        }),
      ),
      http.get('/api/health/runtime', () =>
        HttpResponse.json({
          status: 'ready',
          sqlite: { status: 'ready' },
          lancedb: { status: 'ready' },
          embedding: { status: 'ready' },
          reranker: { status: 'ready' },
          default_model: { status: 'ready' },
          errors: [],
        }),
      ),
      http.get('/api/ohlcv/:ticker', async ({ params, request }) => {
        requestedUrls.push(new URL(request.url));
        await delay(100);

        return HttpResponse.json({
          symbol: params.ticker,
          candles: [],
          count: 0,
        });
      }),
    );

    render(<App />);

    expect(await screen.findByText(/loading ohlcv/i)).toBeInTheDocument();
    expect(await screen.findByText(/no ohlcv data for this range/i)).toBeInTheDocument();
    expect(screen.getByText(/^selected candle summary:/i, { selector: 'strong' })).toBeInTheDocument();
    expect(requestedUrls).toHaveLength(1);
    expect(requestedUrls[0].pathname).toBe('/api/ohlcv/AAPL');
    expect(requestedUrls[0].searchParams.get('start_date')).toBe('2024-01-02');
    expect(requestedUrls[0].searchParams.get('end_date')).toBe('2024-03-29');
  });
});
