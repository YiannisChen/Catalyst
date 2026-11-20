import type { Time } from 'lightweight-charts';
import type { PriceRow } from './PriceChart';
import { tokens } from '@/styles/tokens';

export function day(date: string): Time {
  return date;
}

export function withAlpha(hex: string, alpha: number): string {
  const value = hex.replace('#', '');
  const red = Number.parseInt(value.slice(0, 2), 16);
  const green = Number.parseInt(value.slice(2, 4), 16);
  const blue = Number.parseInt(value.slice(4, 6), 16);
  return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
}

export function shortDate(value: string): string {
  return new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' }).format(
    new Date(`${value}T00:00:00Z`),
  );
}

export function buildCandleData(rows: PriceRow[]) {
  return rows.map((row) => ({
    time: day(row.date),
    open: row.open,
    high: row.high,
    low: row.low,
    close: row.close,
  }));
}

export function buildVolumeData(rows: PriceRow[]) {
  return rows.map((row) => ({
    time: day(row.date),
    value: row.volume,
    color: withAlpha(row.close >= row.open ? tokens.up : tokens.down, 0.3),
  }));
}

export function buildMarkerDates(selectableDates?: readonly string[], selectedDate?: string): Set<string> {
  return new Set([...(selectableDates ?? []), ...(selectedDate ? [selectedDate] : [])]);
}

export function formatMove(value: number): string {
  return ` · ${value < 0 ? '−' : '+'}${Math.abs(value).toFixed(2)}%`;
}
