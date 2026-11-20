import type React from 'react';
import { AbsoluteFill, Easing, OffthreadVideo, interpolate, staticFile, useCurrentFrame, useVideoConfig } from 'remotion';

/** A region of the recorded page in CSS pixels (the recordings are 1600×900 CSS at 2×). */
export type Rect = { x: number; y: number; w: number; h: number };
export const PAGE_W = 1600;
export const PAGE_H = 900;

/** Where the whole page sits in the 1920×1080 frame: always the full page, never cropped. A caption bar sits below it. */
export const PAGE_SCALE = 1.075;
export const PAGE_X = (1920 - PAGE_W * PAGE_SCALE) / 2;
export const PAGE_Y = 18;
export const BAR_Y = PAGE_Y + PAGE_H * PAGE_SCALE + 14;

/** Per-frame element rects recorded with the take (docs/demo/tools/regions.js): `t` is seconds into the source video. */
export type RegionTrack = { t: number; r: Record<string, Rect> }[];

/**
 * A spotlight keyframe from `at` seconds of this shot's output time: either a fixed rect, or a named region
 * looked up in the recorded track at the current source time (so it follows the element as it grows or moves).
 */
export type SpotKey = { at: number; rect?: Rect; region?: string; fit?: (r: Rect) => Rect };

export type ShotProps = { src: string; from: number; to: number; rate?: number; spots?: SpotKey[]; track?: RegionTrack };

export function shotSeconds({ from, to, rate = 1 }: Pick<ShotProps, 'from' | 'to' | 'rate'>): number {
  return (to - from) / rate;
}

const ease = Easing.bezier(0.4, 0, 0.2, 1);
const MOVE = 0.45; // seconds a spotlight takes to move to its next target
const PAD = 9; // space between the outline and the element it marks
const RADIUS = 12;
const DIM = 0.4; // how dark the rest of the page goes

function lookup(track: RegionTrack | undefined, name: string, t: number): Rect | null {
  if (!track?.length) return null;
  let lo = 0;
  let hi = track.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (track[mid]!.t <= t) lo = mid;
    else hi = mid - 1;
  }
  // Keep the last known rect for a moment if the element is briefly missing (a re-render).
  for (let i = lo; i >= Math.max(0, lo - 6); i -= 1) {
    const r = track[i]!.r[name];
    if (r) return r;
  }
  return null;
}

function resolve(key: SpotKey, track: RegionTrack | undefined, srcT: number): Rect | null {
  const r = key.rect ?? (key.region ? lookup(track, key.region, srcT) : null);
  return r && key.fit ? key.fit(r) : r;
}

/** The outline box: the element grown by PAD, kept inside the page so no side is cut off. */
function outline(r: Rect): Rect {
  const x0 = Math.max(3, r.x - PAD);
  const y0 = Math.max(3, r.y - PAD);
  const x1 = Math.min(PAGE_W - 3, r.x + r.w + PAD);
  const y1 = Math.min(PAGE_H - 3, r.y + r.h + PAD);
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

const mix = (a: Rect, b: Rect, k: number): Rect => ({
  x: a.x + (b.x - a.x) * k,
  y: a.y + (b.y - a.y) * k,
  w: a.w + (b.w - a.w) * k,
  h: a.h + (b.h - a.h) * k,
});

/** One recorded take shown whole, with a spotlight on the part each step is about. */
export const Shot: React.FC<ShotProps> = ({ src, from, to, rate = 1, spots = [], track }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const t = frame / fps;
  const srcT = from + t * rate;
  let idx = -1;
  spots.forEach((s, i) => {
    if (s.at <= t) idx = i;
  });
  let box: Rect | null = null;
  let strength = 0;
  if (idx >= 0) {
    const cur = resolve(spots[idx]!, track, srcT);
    const prev = idx > 0 ? resolve(spots[idx - 1]!, track, srcT) : null;
    const k = ease(Math.min(1, (t - spots[idx]!.at) / MOVE));
    if (cur && prev) {
      box = mix(outline(prev), outline(cur), k);
      strength = 1;
    } else if (cur) {
      box = outline(cur);
      strength = k;
    }
  }
  const opacity = interpolate(frame, [0, 6], [0, 1], { extrapolateRight: 'clamp' });
  return (
    <AbsoluteFill style={{ backgroundColor: '#05070A', opacity }}>
      <div
        style={{
          position: 'absolute',
          left: PAGE_X,
          top: PAGE_Y,
          width: PAGE_W,
          height: PAGE_H,
          transformOrigin: '0 0',
          transform: `scale(${PAGE_SCALE})`,
          borderRadius: 8,
          overflow: 'hidden',
          boxShadow: '0 0 0 1px rgba(255,255,255,0.06), 0 24px 60px rgba(0,0,0,0.6)',
        }}
      >
        <OffthreadVideo src={staticFile(src)} startFrom={Math.round(from * fps)} endAt={Math.round(to * fps)} playbackRate={rate} muted style={{ width: PAGE_W, height: PAGE_H }} />
        {box && (
          <svg width={PAGE_W} height={PAGE_H} style={{ position: 'absolute', inset: 0 }}>
            <defs>
              <mask id="hole">
                <rect width={PAGE_W} height={PAGE_H} fill="white" />
                <rect x={box.x} y={box.y} width={box.w} height={box.h} rx={RADIUS} fill="black" />
              </mask>
              <filter id="glow" x="-10%" y="-10%" width="120%" height="120%">
                <feGaussianBlur stdDeviation="6" />
              </filter>
            </defs>
            <rect width={PAGE_W} height={PAGE_H} fill="#03050A" opacity={DIM * strength} mask="url(#hole)" />
            <rect x={box.x} y={box.y} width={box.w} height={box.h} rx={RADIUS} fill="none" stroke="#F2A93B" strokeWidth={4} opacity={0.22 * strength} filter="url(#glow)" />
            <rect x={box.x} y={box.y} width={box.w} height={box.h} rx={RADIUS} fill="none" stroke="#F2A93B" strokeWidth={1.5} opacity={0.9 * strength} />
          </svg>
        )}
      </div>
    </AbsoluteFill>
  );
};
