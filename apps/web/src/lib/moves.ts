export interface MoveCandle {
  date: string;
  close?: number | null;
}
export interface LargeMove {
  date: string;
  movePct: number;
}

/** Close-to-close moves of at least `threshold` (0.05 = 5%), newest first. */
export function largeMoves(candles: MoveCandle[], threshold = 0.05): LargeMove[] {
  const sorted = [...candles].filter((c) => c.close != null).sort((a, b) => a.date.localeCompare(b.date));
  const moves: LargeMove[] = [];
  for (let i = 1; i < sorted.length; i += 1) {
    const previous = sorted[i - 1]?.close;
    const current = sorted[i];
    if (!current || current.close == null || !previous) continue;
    const movePct = (current.close / previous - 1) * 100;
    if (Math.abs(movePct) >= threshold * 100) moves.push({ date: current.date, movePct });
  }
  return moves.reverse();
}
