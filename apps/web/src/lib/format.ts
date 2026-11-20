export function formatPct(value: number, digits = 2): string {
  if (value === 0) return `${(0).toFixed(digits)}%`;
  return `${value > 0 ? '+' : '−'}${Math.abs(value).toFixed(digits)}%`;
}
export function formatUsd(value: number): string {
  const digits = Math.abs(value) < 0.01 && value !== 0 ? 3 : 2;
  return `$${value.toFixed(digits)}`;
}
export function formatSeconds(value: number): string {
  return `${value.toFixed(1)} s`;
}
function parseInstant(value: string): Date {
  const normalized = value.includes('T') ? value : value.replace(' ', 'T').replace(/ UTC$/, 'Z');
  return new Date(normalized);
}
/** "12:02 PM ET" */
export function formatEtTime(value: string): string {
  const date = parseInstant(value);
  if (!Number.isFinite(date.getTime())) return '';
  return `${new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', hour: 'numeric', minute: '2-digit' }).format(date)} ET`;
}
export function formatCount(value: number): string {
  return new Intl.NumberFormat('en-US').format(value);
}
export function formatCompanyName(value: string): string {
  const suffixes = new Set(['INC', 'CORP', 'CO', 'LTD', 'PLC']);
  // SEC titles carry a state of incorporation suffix such as "/DE" or "/MA/"; drop it.
  return value
    .replace(/\s*\/[A-Za-z]{2}\/?\s*$/, '')
    .split(/(\s+|&)/)
    .map((part) => {
      if (!part || /^\s+$/.test(part) || part === '&') return part;
      const upper = part.toUpperCase();
      if (suffixes.has(upper))
        return ({ INC: 'Inc', CORP: 'Corp', CO: 'Co', LTD: 'Ltd', PLC: 'PLC' } as Record<string, string>)[
          upper
        ]!;
      if (part.length <= 3 && part === upper && !/[AEIOU]/.test(part)) return part;
      return part.charAt(0).toUpperCase() + part.slice(1).toLowerCase();
    })
    .join('');
}
/** "Jul 31, 12:02 PM ET" — the one format for a moment in time. */
export function formatEtDateTime(value: string): string {
  const date = parseInstant(value);
  if (!Number.isFinite(date.getTime())) return '';
  const text = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(date);
  return `${text} ET`;
}
/** "Tue, Jun 9" (or "Tue, Jun 9, 2026") for a trading day given as YYYY-MM-DD. */
export function formatDay(isoDate: string, withYear = false): string {
  const date = new Date(`${isoDate.slice(0, 10)}T12:00:00Z`);
  if (!Number.isFinite(date.getTime())) return isoDate;
  return new Intl.DateTimeFormat('en-US', {
    timeZone: 'UTC',
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    ...(withYear ? { year: 'numeric' } : {}),
  }).format(date);
}
/**
 * Drops listing tags such as "( NASDAQ:AAPL )" or "(NYSE: UNH | UNH Price Prediction)" from a headline
 * or summary shown in a list. Never use it on a quote shown as the source's words.
 */
export function cleanListingTags(value: string): string {
  return value
    .replace(/\s*\(\s*(?:NASDAQ|NYSE|NYSEARCA|NYSEAMERICAN|AMEX|OTC|CBOE|TSX)\s*:[^)]*\)/gi, '')
    .replace(/\s+([,.;:!?])/g, '$1')
    .replace(/\s{2,}/g, ' ')
    .trim();
}
export function decodeEntities(value: string): string {
  const element = document.createElement('textarea');
  element.innerHTML = value;
  return element.value;
}
