import type React from 'react';
import { AbsoluteFill, interpolate, useCurrentFrame } from 'remotion';
import { BAR_Y, PAGE_X } from './Shot';

const AMBER = '#F2A93B';
const INK = '#E7EAF0';
const FONT = '"Geist", "Inter", -apple-system, "Helvetica Neue", Arial, sans-serif';
const DOT = 9;
const GAP = 9;

/**
 * The line under the page: step dots (where this step sits in the clip) and one plain sentence about what is on
 * screen. Both sit at fixed positions so nothing jumps between steps; only the sentence fades.
 */
export const Caption: React.FC<{ text: string; index: number; total: number; durationInFrames: number }> = ({ text, index, total, durationInFrames }) => {
  const frame = useCurrentFrame();
  const opacity = interpolate(frame, [0, 8, durationInFrames - 6, durationInFrames], [0, 1, 1, 0], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const grow = interpolate(frame, [0, 10], [0, 1], { extrapolateRight: 'clamp' });
  const dotsWidth = total * DOT + (total - 1) * GAP;
  return (
    <AbsoluteFill style={{ pointerEvents: 'none', fontFamily: FONT }}>
      <div style={{ position: 'absolute', top: BAR_Y + 12, left: PAGE_X + 6, display: 'flex', gap: GAP, alignItems: 'center', height: 16 }}>
        {Array.from({ length: total }, (_, i) => {
          const current = i === index;
          const done = i < index;
          return (
            <span
              key={i}
              style={{
                width: current ? DOT + 14 * grow : DOT,
                height: DOT,
                borderRadius: DOT,
                background: current || done ? AMBER : 'rgba(231,234,240,0.22)',
                opacity: done ? 0.55 : 1,
              }}
            />
          );
        })}
      </div>
      <div
        style={{
          position: 'absolute',
          top: BAR_Y,
          left: PAGE_X + 6 + dotsWidth + 14 + 26,
          color: INK,
          fontSize: 30,
          fontWeight: 500,
          letterSpacing: '-0.005em',
          whiteSpace: 'nowrap',
          opacity,
        }}
      >
        {text}
      </div>
    </AbsoluteFill>
  );
};
