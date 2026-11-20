import { useEffect, useMemo, useState } from 'react';
import type { components } from '@/api/schema';
import { reduceRunEvents } from './progress';

type Replay = components['schemas']['FeaturedReplayDTO'];

export function useReplayStream(replay: Replay | undefined, skipped: boolean) {
  const [count, setCount] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const events = replay?.events ?? [];
  const speed = Number.isFinite(replay?.speed_hint) && (replay?.speed_hint ?? 0) > 0 ? replay!.speed_hint : 1;
  const duration = events.at(-1)?.offset_ms ?? 0;
  const scale = Math.max(1, duration / speed / 20_000);

  useEffect(() => {
    setCount(0);
    setElapsed(0);
    if (!replay || skipped) {
      if (skipped) setCount(events.length);
      return;
    }
    const start = performance.now();
    const timers = events.map(({ offset_ms }, index) =>
      window.setTimeout(() => setCount(index + 1), offset_ms / speed / scale),
    );
    const ticker = window.setInterval(() => setElapsed((performance.now() - start) / 1000), 100);
    const finish = window.setTimeout(
      () => {
        setElapsed(Math.min(20, duration / speed / scale / 1000));
        window.clearInterval(ticker);
      },
      duration / speed / scale,
    );
    return () => {
      timers.forEach(window.clearTimeout);
      window.clearTimeout(finish);
      window.clearInterval(ticker);
    };
  }, [replay, skipped]);

  const shown = events.slice(0, count).map((item) => item.event);
  const progress = useMemo(() => reduceRunEvents(shown), [shown]);
  return { events: shown, progress, status: 'streaming' as const, elapsed, done: count >= events.length };
}
