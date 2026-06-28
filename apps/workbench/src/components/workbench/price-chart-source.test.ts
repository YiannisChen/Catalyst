import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const priceMoveCardSource = readFileSync(
  new URL('./PriceMoveCard.tsx', import.meta.url),
  'utf8',
);
const candlestickChartSource = readFileSync(
  new URL('../chart/CandlestickChart.tsx', import.meta.url),
  'utf8',
);

test('price chart loads OHLCV instead of receiving scenario candles', () => {
  assert.doesNotMatch(priceMoveCardSource, /data=\{demoCase\.candles\}/);
});

test('price chart does not replace failed OHLCV requests with synthetic candles', () => {
  assert.doesNotMatch(candlestickChartSource, /buildMockOhlc/);
});
