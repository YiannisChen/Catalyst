import { delay, http, HttpResponse } from 'msw';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';

import App from '../../App';
import { cleanup, render, screen } from '../../test/render';
import { server } from '../../test/server';

afterEach(() => cleanup());

function useBootstrapHandlers(symbols: string[] = ['AAPL']) {
  server.use(
    http.get('/api/tickers', () => HttpResponse.json({ symbols, count: symbols.length })),
    http.get('/api/range-local', () =>
      HttpResponse.json({
        min_date: '2024-01-02',
        max_date: '2024-03-29',
        ticker_count: symbols.length,
        row_count: 120,
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
    http.get('/api/ohlcv/:ticker', () =>
      HttpResponse.json({
        symbol: 'AAPL',
        candles: [
          {
            symbol: 'AAPL',
            date: '2024-03-29',
            open: 171,
            high: 173,
            low: 170,
            close: 172,
            volume: 1000,
            source: 'fixture',
          },
        ],
        count: 1,
      }),
    ),
  );
}

describe('SelectedDayContext', () => {
  it('submits create run and shows queued status chip', async () => {
    const user = userEvent.setup();
    const createRunBodies: unknown[] = [];

    useBootstrapHandlers();
    server.use(
      http.post('/api/live-runs', async ({ request }) => {
        createRunBodies.push(await request.json());
        await delay(100);

        return HttpResponse.json({
          run_id: 'run_queued',
          status: 'QUEUED',
          queued_at: '2024-03-29T13:30:00Z',
          model_id: 'fixture-model',
          failure: null,
        });
      }),
    );

    render(<App />);

    const runButtons = await screen.findAllByRole('button', { name: /run attribution/i });
    expect(runButtons).toHaveLength(1);

    await user.click(runButtons[0]);

    expect(await screen.findByText(/queued/i, { selector: '.status-chip' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /run attribution/i })).toBeDisabled();
    expect(createRunBodies).toEqual([
      {
        ticker: 'AAPL',
        trade_date: '2024-03-29',
        query: '',
      },
    ]);
  });

  it('disables run attribution when required context is missing', async () => {
    useBootstrapHandlers([]);

    render(<App />);

    expect(await screen.findByRole('button', { name: /run attribution/i })).toBeDisabled();
  });

  it('shows inline feedback when create run fails', async () => {
    const user = userEvent.setup();

    useBootstrapHandlers();
    server.use(
      http.post('/api/live-runs', () => HttpResponse.text('request rejected', { status: 400 })),
    );

    render(<App />);

    await user.click(await screen.findByRole('button', { name: /run attribution/i }));

    expect(await screen.findByText(/api request failed/i)).toBeInTheDocument();
  });
});
