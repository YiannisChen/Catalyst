import { useEffect, useRef } from 'react';

import type { OhlcvCandle } from '../../api/types';

interface UseLightweightChartOptions {
  candles: OhlcvCandle[];
}

export function useLightweightChart({ candles }: UseLightweightChartOptions) {
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!containerRef.current || candles.length === 0) return;

    containerRef.current.dataset.candleCount = String(candles.length);
  }, [candles]);

  return { containerRef };
}
