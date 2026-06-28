import type { AttributionResult, DemoCase, EvidenceItem } from '../../mock/demoCases.ts';

export interface PriceMetrics {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  closeMove: number;
  volume: number;
  intradayRange: number;
  eventWindow: string;
}

export function collectReferencedEvidenceIds(result: AttributionResult): Set<string> {
  return new Set(result.causes.flatMap((cause) => cause.evidenceIds));
}

export function collectAcceptedEvidenceIds(evidence: EvidenceItem[]): Set<string> {
  return new Set(
    evidence
      .filter((e) => e.criticDecision === 'accepted')
      .map((e) => e.id),
  );
}

function formatEventWindow(date: string): string {
  const end = new Date(`${date}T00:00:00Z`);
  const start = new Date(end);
  start.setUTCDate(end.getUTCDate() - 4);
  const month = new Intl.DateTimeFormat('en-US', { month: 'short', timeZone: 'UTC' });
  const startMonth = month.format(start);
  const endMonth = month.format(end);
  const startLabel = `${startMonth} ${start.getUTCDate()}`;
  const endLabel = startMonth === endMonth
    ? `${end.getUTCDate()}, ${end.getUTCFullYear()}`
    : `${endMonth} ${end.getUTCDate()}, ${end.getUTCFullYear()}`;
  return `${startLabel}-${endLabel}`;
}

export function getSelectedDayMetrics(demoCase: DemoCase, date: string | null): PriceMetrics | null {
  if (!date) return null;
  const index = demoCase.candles.findIndex((candle) => candle.date === date);
  if (index < 0) return null;
  const eventCandle = demoCase.candles[index];
  const previousClose = index > 0 ? demoCase.candles[index - 1].close : eventCandle.open;
  return {
    date: eventCandle.date,
    open: eventCandle.open,
    high: eventCandle.high,
    low: eventCandle.low,
    close: eventCandle.close,
    closeMove: ((eventCandle.close - previousClose) / previousClose) * 100,
    volume: eventCandle.volume,
    intradayRange: eventCandle.high - eventCandle.low,
    eventWindow: formatEventWindow(date),
  };
}

export function getCandidateEvidence(demoCase: DemoCase, date: string | null): EvidenceItem[] {
  if (!date) return [];
  return date === demoCase.defaultSelectedDate ? demoCase.candidateEvidence : [];
}

export function resolveAttributionResult(demoCase: DemoCase, date: string): AttributionResult {
  if (date === demoCase.defaultSelectedDate) return demoCase.attributionResult;
  return {
    status: 'INSUFFICIENT',
    label: 'Insufficient Evidence',
    headline: 'No evidence-backed catalyst is available for the selected date.',
    summary: 'This demo scenario has no curated evidence for the selected candle. Assigning a cause would exceed the available evidence.',
    statusReason: 'No candidate evidence is available in the mock event window.',
    groundingRate: 1,
    refused: true,
    causes: [],
  };
}
