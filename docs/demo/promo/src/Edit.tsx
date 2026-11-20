import type React from 'react';
import { AbsoluteFill, Series } from 'remotion';
import { Caption } from './Overlays';
import { Shot, shotSeconds, type Rect, type RegionTrack, type SpotKey } from './Shot';

export const FPS = 30;

import rerunTrack from './regions-rerun.json';

// The analysis take (rerun.mp4): Run again on MSFT Jul 30, recorded with every element's rect per frame.
// Spotlights name a region and follow it as it grows; only the opening uses fixed rects.
const TRACK = rerunTrack as unknown as RegionTrack;
const DAY: Rect = { x: 536, y: 188, w: 56, h: 434 }; // the Jul 30 candle and its date label
const RUN_AGAIN: Rect = { x: 1484, y: 78, w: 88, h: 22 };
// Evidence cards: the recorded rect starts at each card's label; grow it to the cards' borders and the panel width.
const cards = (r: Rect): Rect => ({ x: 26, y: r.y - 19, w: 1169 - 26, h: 900 - (r.y - 19) });

type Beat = {
  name: string;
  shot: { src: string; from: number; to: number; rate?: number; spots: SpotKey[]; track?: RegionTrack };
  caption: string;
};

const s = (at: number, rect: Rect): SpotKey => ({ at, rect });
const r = (at: number, region: string, fit?: (x: Rect) => Rect): SpotKey => ({ at, region, fit });

const PICK: Beat = {
  name: 'pick',
  shot: { src: 'rerun.mp4', from: 0.2, to: 4.6, track: TRACK, spots: [s(0.4, DAY), s(2.4, RUN_AGAIN)] },
  caption: 'MSFT jumped 15.5% on Jul 30. Catalyst works out why.',
};

const KNOWN: Beat = {
  name: 'known',
  shot: { src: 'rerun.mp4', from: 14.6, to: 16.4, rate: 0.75, track: TRACK, spots: [r(0, 'known')] },
  caption: 'First the numbers: MSFT beat its sector by 10 points.',
};

const READ: Beat = {
  name: 'read',
  shot: { src: 'rerun.mp4', from: 16.4, to: 19.3, rate: 0.8, track: TRACK, spots: [r(0, 'reading')] },
  caption: 'Then it reads each headline and filing from before the close.',
};

const CHECK: Beat = {
  name: 'check',
  shot: { src: 'rerun.mp4', from: 19.3, to: 21.0, rate: 0.6, track: TRACK, spots: [r(0, 'checking')] },
  caption: 'The likely cause is checked against the full article.',
};

const ANSWER_BEAT: Beat = {
  name: 'answer',
  shot: { src: 'rerun.mp4', from: 22.2, to: 28.6, track: TRACK, spots: [r(0, 'answer'), r(3.6, 'group')] },
  caption: 'The answer quotes the article, with its source and time.',
};

const EVIDENCE: Beat = {
  name: 'evidence',
  shot: { src: 'rerun.mp4', from: 32.6, to: 39.6, track: TRACK, spots: [r(0, 'timeline'), r(2.6, 'evidence', cards)] },
  caption: 'The articles it relied on are marked in the day’s news.',
};

// Measured from the recorded frames (element bounds; the outline adds its own padding).
// Data center: one real update, recorded as a time-lapse (data-timelapse.mp4: 0–9 s real time, 9–21 s the run at about 170×, then the finished page).
const SOURCES: Rect = { x: 186, y: 178, w: 1228, h: 90 }; // prices, headlines, filings, search index
const UPDATE_BAR: Rect = { x: 189, y: 289, w: 1225, h: 40 }; // schedule and the update button
const PROGRESS: Rect = { x: 190, y: 356, w: 1212, h: 172 }; // items done and the six steps
const ACTIVITY: Rect = { x: 188, y: 553, w: 705, h: 299 }; // live activity
const STEPS_DONE: Rect = { x: 708, y: 390, w: 704, h: 240 }; // every step Done
const RUNS: Rect = { x: 192, y: 526, w: 500, h: 158 }; // the last 14 runs

const DATA_REST: Beat = {
  name: 'data-rest',
  shot: { src: 'data-timelapse.mp4', from: 0.4, to: 9.0, rate: 1.2, spots: [s(0.3, SOURCES), s(4.2, UPDATE_BAR)] },
  caption: 'Catalyst keeps its own prices, filings and news for 102 stocks.',
};

const DATA_RUN: Beat = {
  name: 'data-run',
  shot: { src: 'data-timelapse.mp4', from: 9.0, to: 21.0, spots: [s(0, PROGRESS), s(6.5, ACTIVITY)] },
  caption: 'Every weekday an update runs in six steps. Here, 34 minutes sped up.',
};

const DATA_DONE: Beat = {
  name: 'data-done',
  shot: { src: 'data-timelapse.mp4', from: 21.0, to: 25.0, spots: [s(0, STEPS_DONE), s(2.0, RUNS)] },
  caption: 'When it finishes, the run is logged, even if only part of it worked.',
};

export const CUTS: Record<'readme' | 'data', Beat[]> = {
  readme: [PICK, KNOWN, READ, CHECK, ANSWER_BEAT, EVIDENCE],
  data: [DATA_REST, DATA_RUN, DATA_DONE],
};

function beatFrames(b: Beat): number {
  return Math.round(shotSeconds(b.shot) * FPS);
}

export function cutFrames(cut: keyof typeof CUTS): number {
  return CUTS[cut].reduce((sum, b) => sum + beatFrames(b), 0);
}

export const Edit: React.FC<{ cut: keyof typeof CUTS }> = ({ cut }) => (
  <AbsoluteFill style={{ backgroundColor: '#05070A' }}>
    <Series>
      {CUTS[cut].map((b, i) => {
        const frames = beatFrames(b);
        return (
          <Series.Sequence key={b.name} durationInFrames={frames}>
            <Shot {...b.shot} />
            <Caption text={b.caption} index={i} total={CUTS[cut].length} durationInFrames={frames} />
          </Series.Sequence>
        );
      })}
    </Series>
  </AbsoluteFill>
);
