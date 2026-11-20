# Catalyst promo edit (Remotion)

The demo video and README GIF are edited here from screen recordings of the app.

- `src/Edit.tsx` — the cuts (`readme`, `promo`) as beats: which recorded take, the source time range, speed, camera keyframes and caption.
- `src/Shot.tsx` — one take with a virtual camera that pushes in on the moving part and stays inside the page.
- `src/Overlays.tsx` — caption cards and the title cards.

Every shot shows the whole page; a spotlight (the rest dimmed, a thin amber outline with a soft glow) marks the panel or section each step is about, and under the page sit step dots (where the step falls in the clip) and one plain sentence about what is on screen. The analysis take records every element's on-screen rect with each frame (`../tools/regions.js`), so a spotlight follows its section exactly as it grows; the data take uses rects measured from its frames.

Compositions: `Readme` (about 27 s, `catalyst-demo.gif`) and `Data` (about 23 s, `data-update.gif`), both 1920×1080.

## Re-render

1. Record the takes with `../tools/clips.mjs` (`HIRES=1` for 2× frames): `rerun` (Run again on an explained day, MSFT Jul 30) and `data-timelapse` (resumes a real data update and keeps one frame every 4 s until it finishes).
2. Turn each take's frames into `public/rerun.mp4` (frames held for their recorded times; its rects go to `src/regions-rerun.json`) and `public/data-timelapse.mp4` (the time-lapse: real time up to the click, then the run squeezed to 12 s, then the finished page) (frames with their timestamps, 30 fps; see `../tools/assemble.py` for the concat step).
3. Adjust the source times in `src/Edit.tsx` to the new takes, then:

```bash
npm install
npx remotion studio            # preview
npx remotion render src/index.ts Data out/catalyst-Data.mp4 --codec=h264 --crf=18
```

The GIFs are the renders at 1280 px, 10 fps, 160 colours.
