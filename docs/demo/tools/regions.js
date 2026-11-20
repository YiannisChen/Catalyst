// In-page: the on-screen rect of each panel or section a demo spotlight can mark (CSS px, clipped to the
// viewport below the top bar). Recorded with every frame so a spotlight follows its element exactly.
window.__regions = () => {
  const TOP = 56;
  const rect = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const y0 = Math.max(r.top, TOP);
    const y1 = Math.min(r.bottom, innerHeight);
    if (r.width < 4 || y1 - y0 < 12) return null;
    return { x: r.left, y: y0, w: r.width, h: y1 - y0 };
  };
  const union = (els) => {
    const rs = els.map(rect).filter(Boolean);
    if (!rs.length) return null;
    const x0 = Math.min(...rs.map((r) => r.x)), y0 = Math.min(...rs.map((r) => r.y));
    const x1 = Math.max(...rs.map((r) => r.x + r.w)), y1 = Math.max(...rs.map((r) => r.y + r.h));
    return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
  };
  const leaves = (re) => [...document.querySelectorAll('span,div,h2,h3,p,strong,button')].filter((e) => e.children.length === 0 && re.test(e.textContent.trim()));
  const panelHeader = [...document.querySelectorAll('header.panel-header')].find((h) => /Attribution/.test(h.textContent));
  const panel = panelHeader?.closest('section');
  const chart = leaves(/^Can be explained/)[0]?.closest('section') ?? null;
  const known = document.querySelector('section.run-known');
  const reading = document.querySelector('.run-reading');
  const cause = leaves(/^Possible cause/)[0];
  let checking = null;
  for (let p = cause; p && p !== known; p = p.parentElement) {
    const cs = getComputedStyle(p);
    if (parseFloat(cs.borderLeftWidth) > 0 || parseFloat(cs.borderTopWidth) > 0) { checking = p; break; }
  }
  const knownParts = known ? [...known.children].filter((c) => !c.classList.contains('run-reading') && !(checking && c.contains(checking))) : [];
  const steps = leaves(/^Writing$/)[0]?.closest('ol,ul,div:has(> * + * + * + *)') ?? null;
  const evidence = leaves(/Used as evidence/).map((e) => e.closest('article') ?? e.closest('li') ?? e.parentElement?.parentElement).filter((e) => e && !e.closest('header'));
  const cards = [...document.querySelectorAll('main article')];
  return {
    cases: union(cards),
    apple: rect(cards.find((c) => /Apple falls/.test(c.textContent)) ?? null),
    chart: rect(chart),
    panel: rect(panel),
    ready: union([panelHeader, ...(panel ? [...panel.querySelectorAll('button')].filter((b) => /Explain this move/.test(b.textContent)) : [])].filter(Boolean)),
    status: union([panelHeader, steps].filter(Boolean)),
    known: union(knownParts),
    reading: rect(reading),
    checking: rect(checking),
    answer: rect(document.querySelector('section.polish-answer-cause')),
    group: rect(document.querySelector('section.polish-answer-group')),
    timeline: rect(leaves(/^When the news came out$/)[0]?.closest('.mb-2')),
    evidence: union(evidence),
  };
};
