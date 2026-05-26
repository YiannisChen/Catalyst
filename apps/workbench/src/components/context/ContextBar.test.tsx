import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import App from '../../App';
import { render, screen } from '../../test/render';
import { server } from '../../test/server';

describe('ContextBar', () => {
  it('renders ticker options and runtime health badge from api', async () => {
    const requestedPaths: string[] = [];

    server.use(
      http.get('/api/tickers', ({ request }) => {
        requestedPaths.push(new URL(request.url).pathname);
        return HttpResponse.json({ symbols: ['AAPL', 'MSFT'], count: 2 });
      }),
      http.get('/api/range-local', ({ request }) => {
        requestedPaths.push(new URL(request.url).pathname);
        return HttpResponse.json({
          min_date: '2024-01-02',
          max_date: '2024-03-29',
          ticker_count: 2,
          row_count: 120,
        });
      }),
      http.get('/api/health/runtime', ({ request }) => {
        requestedPaths.push(new URL(request.url).pathname);
        return HttpResponse.json({
          status: 'ready',
          sqlite: { status: 'ready' },
          lancedb: { status: 'ready' },
          embedding: { status: 'ready' },
          reranker: { status: 'ready' },
          default_model: { status: 'ready' },
          errors: [],
        });
      }),
    );

    render(<App />);

    const tickerSelect = await screen.findByRole('combobox', { name: /ticker/i });

    expect(tickerSelect).toBeInTheDocument();
    expect(await screen.findByText(/ready/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/date/i)).toHaveAttribute('min', '2024-01-02');
    expect(screen.getByLabelText(/date/i)).toHaveAttribute('max', '2024-03-29');
    expect(requestedPaths).toEqual(
      expect.arrayContaining(['/api/tickers', '/api/range-local', '/api/health/runtime']),
    );
  });
});
