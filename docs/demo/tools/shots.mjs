// Capture demo screenshots through Chrome's DevTools protocol (no extra packages).
// usage: node shots.mjs <outDir> <shots.json>
import { spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [outDir, listFile] = process.argv.slice(2);
const shots = JSON.parse(readFileSync(listFile, 'utf8'));
const port = 9333;
const chrome = spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', [
  '--headless=new', '--disable-gpu', '--hide-scrollbars', `--remote-debugging-port=${port}`,
  `--user-data-dir=${mkdtempSync(join(tmpdir(), 'cx-shots-'))}`, '--window-size=1920,1300', '--disable-background-timer-throttling', '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows', 'about:blank',
], { stdio: 'ignore' });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let wsUrl;
for (let i = 0; i < 50 && !wsUrl; i += 1) {
  try {
    const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    wsUrl = list.find((t) => t.type === 'page')?.webSocketDebuggerUrl;
  } catch { /* not up yet */ }
  if (!wsUrl) await sleep(200);
}
const ws = new WebSocket(wsUrl);
await new Promise((r) => ws.addEventListener('open', r));
let id = 0;
const pending = new Map();
ws.addEventListener('message', (event) => {
  const msg = JSON.parse(event.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
});
const send = (method, params = {}) => new Promise((resolve) => {
  id += 1; pending.set(id, resolve); ws.send(JSON.stringify({ id, method, params }));
});
const evaluate = async (expression) =>
  (await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })).result?.result?.value;

await send('Page.enable');
for (const shot of shots) {
  await send('Emulation.setDeviceMetricsOverride', { width: shot.width ?? 1440, height: shot.height ?? 900, deviceScaleFactor: shot.scale ?? 2, mobile: false });
  await send('Page.navigate', { url: shot.url });
  await sleep(shot.wait ?? 6000);
  for (const step of shot.steps ?? []) {
    await evaluate(step);
    await sleep(1500);
  }
  const check = shot.expect ? await evaluate(`document.body.innerText.includes(${JSON.stringify(shot.expect)})`) : true;
  const { result } = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(join(outDir, `${shot.name}.png`), Buffer.from(result.data, 'base64'));
  console.log(`${shot.name}: ${check ? 'ok' : 'MISSING ' + shot.expect}`);
}
ws.close();
chrome.kill();
