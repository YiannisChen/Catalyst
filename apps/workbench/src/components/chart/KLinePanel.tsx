import { useEffect, useMemo, useState } from 'react';

import { getOhlcv } from '../../api/client';
import type { OhlcvCandle } from '../../api/types';
import { useLightweightChart } from './useLightweightChart';

interface KLinePanelProps {
  ticker: string;
  startDate: string | null;
  endDate: string | null;
}

type OhlcvState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'empty'; candles: OhlcvCandle[] }
  | { status: 'ready'; candles: OhlcvCandle[] }
  | { status: 'error'; message: string };

export function KLinePanel({ ticker, startDate, endDate }: KLinePanelProps) {
  const [ohlcv, setOhlcv] = useState<OhlcvState>({ status: 'idle' });
  const candles = ohlcv.status === 'ready' || ohlcv.status === 'empty' ? ohlcv.candles : [];
  const { containerRef } = useLightweightChart({ candles });

  useEffect(() => {
    if (!ticker || !startDate || !endDate) {
      setOhlcv({ status: 'idle' });
      return;
    }

    let isActive = true;

    async function loadOhlcv() {
      setOhlcv({ status: 'loading' });

      try {
        const response = await getOhlcv(ticker, { start_date: startDate, end_date: endDate });
        if (!isActive) return;

        setOhlcv(
          response.candles.length === 0
            ? { status: 'empty', candles: response.candles }
            : { status: 'ready', candles: response.candles },
        );
      } catch (error) {
        if (!isActive) return;

        setOhlcv({
          status: 'error',
          message: error instanceof Error ? error.message : 'Unable to load OHLCV data.',
        });
      }
    }

    void loadOhlcv();

    return () => {
      isActive = false;
    };
  }, [endDate, startDate, ticker]);

  const selectedSummary = useMemo(() => {
    if (candles.length === 0) return 'Selected candle summary will appear after chart data loads.';

    const latest = candles[candles.length - 1];
    return `${latest.symbol} ${latest.date}: O ${latest.open ?? '-'} H ${latest.high ?? '-'} L ${
      latest.low ?? '-'
    } C ${latest.close ?? '-'}`;
  }, [candles]);

  return (
    <section className="panel" aria-label="chart-panel">
      <h2>K-Line Panel · {ticker || 'No ticker'}</h2>
      <div className="chart-frame" ref={containerRef} aria-label="candlestick chart area">
        {ohlcv.status === 'idle' ? <span>Select a ticker and date range to load OHLCV.</span> : null}
        {ohlcv.status === 'loading' ? <span>Loading OHLCV data</span> : null}
        {ohlcv.status === 'empty' ? <span>No OHLCV data for this range.</span> : null}
        {ohlcv.status === 'error' ? <span>Unable to load OHLCV data: {ohlcv.message}</span> : null}
        {ohlcv.status === 'ready' ? <span>{candles.length} candles loaded.</span> : null}
      </div>
      <p className="chart-summary" aria-live="polite">
        <strong>Selected candle summary:</strong> {selectedSummary}
      </p>
    </section>
  );
}
