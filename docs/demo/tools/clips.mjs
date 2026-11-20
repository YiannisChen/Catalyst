// Records Catalyst demo clips through Chrome's DevTools protocol: real mouse/keyboard input,
// a drawn cursor, screencast frames with timestamps (one folder per clip).
// usage: node clips.mjs <workDir> [clipName ...]
import { spawn } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [work, ...only] = process.argv.slice(2);
const BASE = 'http://localhost:5177';
const W = 1600;
const H = 900;
const port = 9335;
const chrome = spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', [
  ...(process.env.HEADED === '1' ? ['--new-window', '--window-position=40,40'] : ['--headless=new', '--disable-gpu']), '--hide-scrollbars', '--force-device-scale-factor=2', '--no-first-run', '--no-default-browser-check', '--disable-background-timer-throttling', '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows', `--remote-debugging-port=${port}`,
  `--user-data-dir=${mkdtempSync(join(tmpdir(), 'cx-clips-'))}`, `--window-size=${W},${H}`, 'about:blank',
], { stdio: 'ignore' });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let wsUrl;
for (let i = 0; i < 50 && !wsUrl; i += 1) {
  try {
    const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    wsUrl = list.find((t) => t.type === 'page')?.webSocketDebuggerUrl;
  } catch { /* starting */ }
  if (!wsUrl) await sleep(200);
}
const ws = new WebSocket(wsUrl);
await new Promise((r) => ws.addEventListener('open', r));
let id = 0;
const pending = new Map();
let sink = null; // { dir, frames }
ws.addEventListener('message', (event) => {
  const msg = JSON.parse(event.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); return; }
  if (msg.method === 'Page.screencastFrame') {
    const { data, metadata, sessionId } = msg.params;
    ws.send(JSON.stringify({ id: ++id, method: 'Page.screencastFrameAck', params: { sessionId } }));
    if (!sink || sink.paused) return;
    const name = `f${String(sink.frames.length).padStart(5, '0')}.jpg`;
    writeFileSync(join(sink.dir, name), Buffer.from(data, 'base64'));
    sink.frames.push({ name, t: metadata.timestamp - sink.pausedTotal });
  }
});
const send = (method, params = {}) => new Promise((resolve) => {
  id += 1; pending.set(id, resolve); ws.send(JSON.stringify({ id, method, params }));
});
const evaluate = async (expression) =>
  (await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })).result?.result?.value;

const cursorScript = `
addEventListener('DOMContentLoaded', () => {
  const c = document.createElement('div');
  c.id = '__demo_cursor';
  c.innerHTML = '<svg width="24" height="24" viewBox="0 0 24 24"><path d="M4 2 L4 19 L8.5 14.8 L11.6 21.6 L14.3 20.4 L11.2 13.7 L17.5 13.7 Z" fill="#fff" stroke="#111" stroke-width="1.4" stroke-linejoin="round"/></svg>';
  Object.assign(c.style, { position: 'fixed', left: '${W / 2}px', top: '${H / 2}px', zIndex: 2147483647, pointerEvents: 'none', transform: 'translate(-3px,-2px)', filter: 'drop-shadow(0 1px 2px rgba(0,0,0,.5))' });
  document.body.appendChild(c);
  addEventListener('mousemove', (e) => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; }, true);
  addEventListener('mousedown', (e) => {
    const r = document.createElement('div');
    Object.assign(r.style, { position: 'fixed', left: (e.clientX - 18) + 'px', top: (e.clientY - 18) + 'px', width: '36px', height: '36px', borderRadius: '18px', border: '2px solid #F2A93B', zIndex: 2147483646, pointerEvents: 'none', transition: 'transform .5s ease-out, opacity .5s ease-out' });
    document.body.appendChild(r);
    requestAnimationFrame(() => { r.style.transform = 'scale(1.9)'; r.style.opacity = '0'; });
    setTimeout(() => r.remove(), 650);
  }, true);
});`;

await send('Page.enable');
await send('Emulation.setFocusEmulationEnabled', { enabled: true });
await send('Page.addScriptToEvaluateOnNewDocument', { source: cursorScript });
// Every HIRES frame also records where each spotlight region is on screen (see regions.js).
await send('Page.addScriptToEvaluateOnNewDocument', { source: readFileSync(new URL('./regions.js', import.meta.url), 'utf8') });
await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 2, mobile: false });

let mouse = { x: W / 2, y: H / 2 };
async function moveTo(x, y, ms = 800) {
  const steps = Math.max(10, Math.round(ms / 20));
  const from = { ...mouse };
  for (let i = 1; i <= steps; i += 1) {
    const k = i / steps;
    const e = k < 0.5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2;
    mouse = { x: from.x + (x - from.x) * e, y: from.y + (y - from.y) * e };
    await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: mouse.x, y: mouse.y });
    await sleep(ms / steps);
  }
}
async function click() {
  await send('Input.dispatchMouseEvent', { type: 'mousePressed', x: mouse.x, y: mouse.y, button: 'left', buttons: 1, clickCount: 1 });
  await sleep(70);
  await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: mouse.x, y: mouse.y, button: 'left', buttons: 0, clickCount: 1 });
}
async function wheel(deltaY, ms = 900) {
  const steps = 18;
  for (let i = 0; i < steps; i += 1) {
    await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: mouse.x, y: mouse.y, deltaX: 0, deltaY: deltaY / steps });
    await sleep(ms / steps);
  }
}
async function find(text, { scope = 'body', exact = false, tags = '*' } = {}) {
  return evaluate(`(() => {
    const roots = [...document.querySelectorAll(${JSON.stringify(scope)})];
    const want = ${JSON.stringify(text)};
    const els = roots.flatMap((root) => [...root.querySelectorAll(${JSON.stringify(tags)})]).filter((el) => {
      const t = (el.innerText || '').trim();
      if (!t || (${exact} ? t !== want : !t.includes(want))) return false;
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight;
    });
    els.sort((a, b) => a.innerText.length - b.innerText.length);
    const r = els[0]?.getBoundingClientRect();
    return r ? { x: r.left + r.width / 2, y: r.top + r.height / 2 } : null;
  })()`);
}
async function point(text, opts, ms = 900) {
  const p = await find(text, opts);
  if (!p) { console.log('  not found:', text); return false; }
  await moveTo(p.x, p.y, ms);
  return true;
}
async function clickText(text, opts, ms = 800, expect = null) {
  if (!(await point(text, opts, ms))) return false;
  await sleep(200);
  await click();
  if (expect && !(await waitFor(expect, 1500))) {
    // The real click did not land (seen intermittently under automation): click the same element directly.
    const scope = JSON.stringify(opts?.scope ?? 'body');
    const tags = JSON.stringify(opts?.tags ?? '*');
    await evaluate(`(() => { const els = [...document.querySelectorAll(${scope})].flatMap((r) => [...r.querySelectorAll(${tags})]).filter((el) => (el.innerText || '').trim().includes(${JSON.stringify(text)})); els.sort((a, b) => a.innerText.length - b.innerText.length); els[0]?.click(); })()`);
    console.log('  fallback click:', text, await waitFor(expect, 3000));
  }
  return true;
}
// A clean cut to another page: the reload and loading time are left out of the clip.
async function cut(path, readyText, waitMs = 8000) {
  const pauseStart = Date.now() / 1000;
  if (sink) sink.paused = true;
  await send('Page.navigate', { url: BASE + path });
  if (readyText) await waitFor(readyText, waitMs);
  if (path.includes('run=')) await waitFor('Used as evidence', 20000);
  await sleep(1200);
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: mouse.x, y: mouse.y });
  await sleep(300);
  if (sink) { sink.pausedTotal += Date.now() / 1000 - pauseStart; sink.paused = false; }
}
// Click a control for real, then cut to the page it leads to (route changes do not redraw reliably headless).
async function clickThenCut(text, opts, path, readyText) {
  if (!(await point(text, opts, 850))) return false;
  await sleep(250);
  await click();
  await sleep(350);
  await cut(path, readyText);
  return true;
}
const go = (path) => evaluate(`(() => { history.pushState({}, '', ${JSON.stringify(path)}); dispatchEvent(new PopStateEvent('popstate')); })()`);
async function waitFor(text, timeoutMs) {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    if (await evaluate(`document.body.innerText.includes(${JSON.stringify(text)})`)) return true;
    await sleep(300);
  }
  return false;
}
// Scroll the scrollable ancestor of an element so the element sits near the top, without recording jumps.
const scrollInner = (text, offset = 8) => evaluate(`(() => {
  const n = [...document.querySelectorAll('header,h2,h3')].find((h) => h.textContent.trim().startsWith(${JSON.stringify(text)}));
  if (!n) return false;
  let s = n.parentElement;
  while (s && !(s.scrollHeight > s.clientHeight + 5 && ['auto', 'scroll'].includes(getComputedStyle(s).overflowY))) s = s.parentElement;
  if (s) s.scrollTop += n.getBoundingClientRect().top - s.getBoundingClientRect().top - ${offset};
  return true;
})()`);

// Sweep the chart with the mouse until the hover hint names the day; returns its x.
async function findDayX(label, y = 500) {
  for (let x = 60; x < 1330; x += 3) {
    await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y });
    const hint = await evaluate(`document.querySelector('.chart-hint')?.innerText || ''`);
    if (hint.startsWith(label + ' ') || hint.startsWith(label + '\n') || hint.includes(label + ' ·')) return x;
  }
  return null;
}

const RUN = {
  confirmed: '/?ticker=AAPL&date=2026-07-31&run=1f0542dcf51c4f8ab1a8a1cac78efe2e',
  reported: '/?ticker=GS&date=2026-05-20&run=ea48427c67dd4c688ab4a1e8323b47f6',
  group: '/?ticker=AMZN&date=2026-11-05&run=c305a48f758d47338d1f7a15a0549e7f',
  nothing: '/?ticker=INTC&date=2026-01-26&run=9243bd576fe746bdb9c31a2381107100',
};
const LIVE_DAY = process.env.LIVE_DAY || '2026-06-09';
const LIVE_TICKER = process.env.LIVE_TICKER || 'AAPL';

async function prepare(path, waitMs = 6000) {
  await send('Page.navigate', { url: BASE + path });
  await sleep(waitMs);
  // Workbench pages: start only once the news list has drawn (it can lag behind the answer).
  if (path.startsWith('/?') && path.includes('date=')) {
    const t = Date.now();
    const ok = await waitFor('Used as evidence', 20000);
    console.log(`  news ready: ${ok} after ${((Date.now() - t) / 1000).toFixed(1)} s`);
  }
}
async function warm(paths) {
  for (const path of paths) await prepare(path, 4000);
}
async function record(name, prep, actions) {
  if (only.length && !only.includes(name)) return;
  console.log('clip', name);
  await prep();
  const dir = join(work, 'clips', name);
  rmSync(dir, { recursive: true, force: true });
  mkdirSync(dir, { recursive: true });
  const hires = process.env.HIRES === '1';
  if (!hires) await send('Page.startScreencast', { format: 'jpeg', quality: 90, maxWidth: W * 2, maxHeight: H * 2, everyNthFrame: 1 });
  await sleep(400);
  sink = { dir, frames: [], paused: false, pausedTotal: 0 };
  // HIRES=1: true 2x frames from back-to-back screenshots (the screencast only delivers 1x).
  let grabbing = hires;
  const grabber = (async () => {
    while (grabbing) {
      if (sink && !sink.paused) {
        const t = Date.now() / 1000 - sink.pausedTotal;
        const shot = await send('Page.captureScreenshot', { format: 'jpeg', quality: 92, optimizeForSpeed: true });
        if (sink && !sink.paused && shot.result) {
          const name = `f${String(sink.frames.length).padStart(5, '0')}.jpg`;
          writeFileSync(join(sink.dir, name), Buffer.from(shot.result.data, 'base64'));
          const regions = await evaluate('window.__regions ? JSON.stringify(window.__regions()) : null');
          sink.frames.push({ name, t, regions: regions ? JSON.parse(regions) : null });
        }
        if (sink?.interval) await sleep(sink.interval); // time-lapse: one frame every `interval` ms
      } else await sleep(50);
    }
  })();
  const start = Date.now() / 1000;
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: mouse.x, y: mouse.y });
  await sleep(1200);
  await actions();
  await sleep(1500);
  const end = Date.now() / 1000 - sink.pausedTotal;
  grabbing = false;
  await grabber;
  const frames = sink.frames;
  sink = null;
  if (!hires) await send('Page.stopScreencast');
  writeFileSync(join(dir, 'timeline.json'), JSON.stringify({ start, end, frames }));
  console.log(`  ${frames.length} frames, ${(end - start).toFixed(1)} s`);
}

if (process.env.NOWARM !== '1') await warm([RUN.confirmed, RUN.reported, RUN.group, RUN.nothing, '/featured', '/data', '/settings', `/?ticker=AAPL&date=${LIVE_DAY}`]);

// C1 · Pick a day, read the ready panel, scroll to its news
let dayX = null;
await record('c1-pick-a-day', async () => {
  await prepare('/', 7000);
  dayX = await findDayX(process.env.LIVE_LABEL || 'Jun 9');
  console.log('  day x:', dayX);
  mouse = { x: 1150, y: 300 };
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: mouse.x, y: mouse.y });
}, async () => {
  for (const x of [260, 480, 700, 920, 1080]) await moveTo(x, 470, 750);
  await sleep(400);
  await moveTo(780, 818, 900); // the lane of explainable days
  await sleep(1300);
  if (dayX == null) await go(`/?ticker=AAPL&date=${LIVE_DAY}`);
  else { await moveTo(dayX, 500, 1000); await sleep(900); await click(); }
  await waitFor('Explain this move', 8000);
  await sleep(1200);
  await point('WHAT CATALYST WILL READ', {}, 1000);
  await sleep(2200);
  await moveTo(700, 680, 900);
  await wheel(560, 1400);
  await sleep(1500);
  await point('Overnight', {}, 900);
  await sleep(1400);
  await point('Session ·', {}, 900);
  await sleep(1400);
  await point('Used as evidence', {}, 900);
  await sleep(1600);
});

// C2 · A live analysis, from the button to the answer
await record('c2-live-run', async () => { await prepare(`/?ticker=AAPL&date=${LIVE_DAY}`, 7000); mouse = { x: 900, y: 500 }; }, async () => {
  await sleep(600);
  await clickText('Explain this move', { tags: 'button' }, 1000);
  // The run's URL appears once it is accepted; reload onto it so the live panel draws.
  let runPath = null;
  for (let i = 0; i < 40 && !runPath; i += 1) {
    await sleep(250);
    const search = await evaluate('location.search');
    if (search.includes('run=')) runPath = '/' + search;
  }
  console.log('  run:', runPath);
  if (runPath) await cut(runPath, 'Attribution', 6000);
  const done = await waitFor('THE CAUSE', 90000) || await waitFor('WHAT ONE SOURCE SAYS', 1000)
    || await waitFor('WHAT CATALYST FOUND', 1000) || await waitFor('AGAINST ITS GROUP', 1000);
  console.log('  answer shown:', done);
  await sleep(1500);
  await moveTo(1380, 330, 1000);
  await sleep(3000);
});

// C2 tail · the finished answer once its news has loaded (joined to C2 without a fade)
await record('c2-tail', async () => { await prepare(process.env.LIVE_RUN || '/', 8000); await waitFor('Used as evidence', 8000); mouse = { x: 1380, y: 330 }; await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: mouse.x, y: mouse.y }); }, async () => {
  await sleep(800);
  await point('Confirmed', { exact: true }, 900);
  await sleep(1500);
  await moveTo(700, 760, 1200);
  await sleep(2200);
});

// RUN · the README GIF: one analysis from the button to the answer, cropped to the panel later
await record('run-gif', async () => {
  await prepare(`/?ticker=${LIVE_TICKER}&date=${LIVE_DAY}`, 7000);
  await waitFor('Explain this move', 8000);
  mouse = { x: 1380, y: 560 };
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: mouse.x, y: mouse.y });
}, async () => {
  await sleep(500);
  await clickText('Explain this move', { tags: 'button' }, 900);
  let runPath = null;
  for (let i = 0; i < 40 && !runPath; i += 1) {
    await sleep(250);
    const search = await evaluate('location.search');
    if (search.includes('run=')) runPath = '/' + search;
  }
  console.log('  run:', runPath);
  if (runPath) await cut(runPath, 'Attribution', 6000);
  await moveTo(1500, 140, 700);
  // The header offers "Run again" only once the answer is out (it shows "Stop" while running).
  const done = await waitFor('Run again', 90000);
  console.log('  answer shown:', done, runPath);
  await sleep(800);
  await point('IN THE SOURCE', {}, 900);
  await sleep(4000);
});

// DATA · a real data update starting and moving through its steps (only the middle is used)
await record('data-update', async () => {
  await prepare('/data', 6000);
  console.log('  button ready:', await waitFor('Resume update', 60000) || await waitFor('Run update now', 1000));
  mouse = { x: 900, y: 420 };
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: mouse.x, y: mouse.y });
}, async () => {
  await sleep(600);
  await clickText('Resume update', { tags: 'button' }, 900, 'Start an update now');
  await sleep(1200);
  await clickText('Resume update', { tags: 'button' }, 700, 'Update running');
  console.log('  running:', await waitFor('Update running', 15000));
  for (let i = 0; i < 6; i += 1) {
    await point('NEXT RUN', {}, 1400);
    await sleep(6000);
    await point('Status', {}, 1400);
    await sleep(6000);
  }
});

// DATA TIMELAPSE · the data page at rest, Resume update, then its progress until the job finishes.
// Needs HIRES=1 (the time-lapse throttles the screenshot loop). Frames come every 4 s while it runs.
await record('data-timelapse', async () => {
  await prepare('/data', 6000);
  console.log('  button ready:', await waitFor('Resume update', 60000) || await waitFor('Run update now', 1000));
  mouse = { x: 1380, y: 300 };
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: mouse.x, y: mouse.y });
}, async () => {
  await sleep(2500); // the page at rest
  await clickText('Resume update', { tags: 'button' }, 900, 'Start an update now');
  await sleep(1200);
  await clickText('Resume update', { tags: 'button' }, 700, 'Update running');
  console.log('  running:', await waitFor('Update running', 15000));
  await sleep(4000);
  sink.interval = 4000;
  const started = Date.now();
  for (;;) {
    await sleep(15000);
    const job = await (await fetch('http://127.0.0.1:8001/api/data/jobs/latest')).json().catch(() => null);
    if (job && !job.running && job.status !== 'RUNNING') { console.log('  finished:', job.status, job.items_done, '/', job.items_total); break; }
    if (Date.now() - started > 4 * 3600 * 1000) { console.log('  gave up after 4 h'); break; }
  }
  sink.interval = 0;
  await sleep(6000); // the page settles on its finished state
});

// REPLAY · the Featured page, Replay on the Apple case: the saved run streams again step by step (free, no model call),
// then the finished answer with its evidence marked in the day's news. Every frame carries the region rects.
const REPLAY_CASE = '/?ticker=AAPL&date=2026-06-25&case=aapl-2026-06-25';
const REPLAY_RUN = '/?ticker=AAPL&date=2026-06-25&run=6ee6c38af2fb48ee81a5a176c18b418b';
await record('replay', async () => {
  await prepare('/featured', 6000);
  console.log('  featured ready:', await waitFor('Apple falls', 20000));
  mouse = { x: 800, y: 300 };
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: mouse.x, y: mouse.y });
}, async () => {
  await sleep(1800);
  const p = await evaluate(`(() => { const c = [...document.querySelectorAll('main article')].find((a) => /Apple falls/.test(a.textContent)); const b = c && [...c.querySelectorAll('button')].find((x) => x.textContent.trim() === 'Replay'); const r = b?.getBoundingClientRect(); return r ? { x: r.left + r.width / 2, y: r.top + r.height / 2 } : null; })()`);
  console.log('  replay button:', p);
  await moveTo(p.x, p.y, 1100);
  await sleep(300);
  await click();
  await sleep(300);
  await cut(REPLAY_CASE, 'Replay ·', 15000);
  await moveTo(1420, 260, 900);
  console.log('  replay done:', await waitFor('Confirmed', 60000));
  await sleep(3500);
  await cut(REPLAY_RUN, 'Used as evidence', 15000);
  await sleep(1500);
  await moveTo(700, 820, 1000);
  await wheel(520, 1500);
  await sleep(2500);
  await point('Used as evidence', { tags: 'span' }, 900);
  await sleep(3000);
});

// RERUN · an already explained day (MSFT Jul 30): Run again streams a fresh analysis with every read verdict,
// then the answer and its evidence in the day's news. Every frame carries the region rects (regions.js).
const RERUN_DAY = '/?ticker=MSFT&date=2026-07-30';
await record('rerun', async () => {
  await prepare(RERUN_DAY, 7000);
  console.log('  answer shown:', await waitFor('Run again', 30000));
  mouse = { x: 900, y: 420 };
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: mouse.x, y: mouse.y });
}, async () => {
  await sleep(1800);
  const before = Date.now() / 1000;
  await clickText('Run again', { tags: 'button', scope: 'header' }, 1100, 'Stop');
  let runId = null;
  for (let i = 0; i < 40 && !runId; i += 1) {
    await sleep(300);
    const list = await (await fetch('http://127.0.0.1:8001/api/live-runs?limit=5')).json();
    runId = list.items.find((r) => r.ticker === 'MSFT' && r.session_date === '2026-07-30' && Date.parse(r.created_at) / 1000 > before - 5)?.run_id ?? null;
  }
  console.log('  run:', runId);
  const runPath = `${RERUN_DAY}&run=${runId}`;
  await cut(runPath, 'Stop', 8000);
  await moveTo(1420, 300, 900);
  console.log('  answer:', await waitFor('Run again', 120000));
  await sleep(3000);
  await cut(runPath, 'Used as evidence', 15000);
  await sleep(1500);
  await moveTo(700, 820, 1000);
  await wheel(520, 1500);
  await sleep(2500);
  await point('Used as evidence', { tags: 'span' }, 900);
  await sleep(3000);
});

// RUN FULL · full screen at 2x: click Explain, the live panel streams, then the news shows its evidence
await record('run-full', async () => {
  await prepare(`/?ticker=${LIVE_TICKER}&date=${LIVE_DAY}`, 7000);
  console.log('  ready:', await waitFor('Explain this move', 30000));
  mouse = { x: 900, y: 420 };
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: mouse.x, y: mouse.y });
}, async () => {
  await sleep(1200);
  const before = Date.now() / 1000;
  await clickText('Explain this move', { tags: 'button' }, 1100, 'Stop');
  // Find the new run through the API (the page URL can lag under automation), then show its live view.
  let runId = null;
  for (let i = 0; i < 40 && !runId; i += 1) {
    await sleep(300);
    const list = await (await fetch('http://127.0.0.1:8001/api/live-runs?limit=5')).json();
    runId = list.items.find((r) => r.ticker === LIVE_TICKER && r.session_date === LIVE_DAY && Date.parse(r.created_at) / 1000 > before - 5)?.run_id ?? null;
  }
  console.log('  run:', runId);
  const runPath = `/?ticker=${LIVE_TICKER}&date=${LIVE_DAY}&run=${runId}`;
  await cut(runPath, 'Stop', 8000);
  await moveTo(1420, 300, 900);
  console.log('  answer:', await waitFor('Run again', 120000));
  await sleep(2500);
  // Reload so the news list marks the evidence this run used.
  await cut(runPath, 'Used as evidence', 15000);
  await sleep(1500);
  await moveTo(700, 820, 1000);
  await wheel(520, 1500);
  await sleep(2500);
  await point('Used as evidence', { tags: 'span' }, 900);
  await sleep(3000);
});

// C3 · A confirmed answer and how it was reached
await record('c3-confirmed', async () => { await prepare(RUN.confirmed, 6000); mouse = { x: 900, y: 450 }; }, async () => {
  await point('Confirmed', { exact: true }, 900);
  await sleep(1300);
  await point('Apple said it sees fourth-quarter', {}, 1000);
  await sleep(2600);
  await point('Peers (2)', {}, 900);
  await sleep(1800);
  await clickThenCut('Process', { tags: 'button' }, RUN.confirmed + '&tab=process', 'read in full');
  await sleep(1200);
  await point('read in full', {}, 900);
  await sleep(1200);
  await point('states the cause', {}, 700);
  await sleep(1400);
  await point('Inside the window', {}, 900);
  await sleep(1600);
});

// C4 · The other three results
await record('c4-other-results', async () => { await prepare(RUN.reported, 6000); mouse = { x: 1000, y: 400 }; }, async () => {
  await point('Reported', { exact: true }, 900);
  await sleep(2600);
  await cut(RUN.group, 'With its group');
  await point('With its group', { exact: true }, 900);
  await sleep(2600);
  await cut(RUN.nothing, 'No clear cause');
  await point('No clear cause', { exact: true }, 900);
  await sleep(2600);
});

// C5 · Models and keys: switch from the top bar, a wrong key is refused
await record('c5-models-and-keys', async () => { await prepare(RUN.confirmed, 6000); mouse = { x: 800, y: 300 }; }, async () => {
  await clickText('server key', { scope: 'header', tags: 'button' }, 1000);
  await sleep(2200);
  console.log('  menu open:', await evaluate(`!!document.querySelector('.analysis-model-menu')`));
  await clickThenCut('Models and keys in Settings', {}, '/settings', 'Spending limits');
  await sleep(800);
  console.log('  at:', await evaluate('location.pathname'), await evaluate(`!!document.querySelector('#models')`));
  await clickText('OpenAI', { scope: '#models', tags: 'button' }, 900);
  await sleep(900);
  const input = await evaluate(`(() => { const i = document.querySelector('#models input[type=password]'); const r = i.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; })()`);
  await moveTo(input.x, input.y, 800);
  await click();
  for (const ch of 'sk-invalid-test-0000000000') { await send('Input.insertText', { text: ch }); await sleep(40); }
  await sleep(400);
  await clickText('Check key', { tags: 'button' }, 700);
  await waitFor('rejected', 15000);
  await sleep(2400);
});

// C6 · Featured cases and the data page
await record('c6-featured-and-data', async () => { await prepare('/featured', 5000); mouse = { x: 700, y: 300 }; }, async () => {
  await moveTo(450, 330, 900);
  await sleep(1200);
  await moveTo(900, 520, 900);
  await sleep(1500);
  await clickThenCut('Data', { scope: 'header', tags: 'a' }, '/data', 'COMPANY HEADLINES');
  await sleep(1200);
  await point('PRICES', {}, 900);
  await sleep(1200);
  await point('COMPANY HEADLINES', {}, 700);
  await sleep(1600);
});

ws.close();
chrome.kill();
