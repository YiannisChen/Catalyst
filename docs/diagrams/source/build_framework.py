"""Generate docs/diagrams/catalyst-framework.svg, the README's main figure.

    python docs/diagrams/source/build_framework.py

The figure tells one analysis as a small newsroom: data comes in on the left,
an AI research desk proposes a reason and gets a second opinion, an editor
applies four fixed rules, and the reader sees the answer on the right.

The worked example is a real featured case (Oracle, 2025-12-19, Confirmed).
The candles are ORCL daily bars from the served market store. Source logos are
in ./logos (see docs/diagrams/README.md for where each one comes from).
"""

from __future__ import annotations

import base64
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "catalyst-framework.svg"

FONT = "Inter, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
INK = "#1F2430"
SUB = "#5B6475"
FAINT = "#8A93A3"
LINE = "#8A93A3"
GREEN = "#1F9D63"
RED = "#E5484D"
CREAM = "#FCEBC7"
CREAM_LINE = "#F0D9A8"

# ORCL daily bars, 2025-10-24 .. 2025-12-19 (open, high, low, close), from the served market store.
ORCL = [
    (285.92, 287.19, 282.25, 283.33),
    (286.57, 287.0, 279.81, 281.4),
    (283.0, 285.46, 280.54, 280.83),
    (281.46, 281.62, 271.35, 275.3),
    (268.74, 269.76, 256.28, 256.89),
    (259.51, 265.81, 256.71, 262.61),
    (265.24, 265.62, 255.23, 257.85),
    (249.98, 255.28, 245.19, 248.17),
    (247.91, 252.4, 247.03, 250.31),
    (247.94, 249.07, 239.29, 243.8),
    (239.0, 240.4, 232.35, 239.26),
    (245.95, 247.24, 237.07, 240.83),
    (236.23, 237.45, 228.89, 236.15),
    (236.74, 236.81, 226.17, 226.99),
    (224.99, 225.16, 215.22, 217.57),
    (210.83, 227.05, 210.77, 222.85),
    (218.21, 222.8, 215.56, 219.86),
    (216.21, 223.7, 214.5, 220.49),
    (219.06, 228.15, 217.31, 225.53),
    (231.16, 234.0, 210.3, 210.69),
    (207.08, 208.31, 193.55, 198.76),
    (196.86, 203.82, 194.3, 200.28),
    (194.08, 199.41, 185.63, 197.03),
    (209.5, 210.0, 202.24, 204.96),
    (199.51, 202.34, 197.61, 201.95),
    (200.5, 203.5, 196.7, 200.94),
    (205.14, 209.7, 200.0, 201.1),
    (200.33, 207.92, 199.4, 207.73),
    (207.86, 215.41, 206.32, 214.33),
    (219.0, 219.5, 213.62, 217.58),
    (221.35, 224.75, 216.91, 220.54),
    (220.03, 222.62, 218.28, 221.53),
    (221.8, 225.32, 217.58, 223.01),
    (190.62, 201.99, 186.23, 198.85),
    (196.37, 197.85, 185.98, 189.97),
    (188.29, 188.97, 181.4, 184.92),
    (184.7, 189.8, 184.49, 188.65),
    (183.31, 186.5, 177.07, 178.46),
    (182.72, 184.76, 178.58, 180.03),
    (188.39, 195.12, 188.12, 191.97),
]

ICONS = {
    "market": '<path d="M4 9.5 5.5 4h13L20 9.5"/><path d="M4 9.5a2.7 2.7 0 0 0 5.3 0 2.7 2.7 0 0 0 5.4 0 2.7 2.7 0 0 0 5.3 0"/><path d="M5.5 11.5V20h13v-8.5"/><path d="M10 20v-4.5h4V20"/>',
    "peers": '<path d="M4 20V10M9.3 20V6M14.6 20v-8M20 20V4"/>',
    "news": '<rect x="3" y="4.5" width="15" height="15" rx="2"/><path d="M18 8.5h2.5V18a1.5 1.5 0 0 1-3 0"/><path d="M6.5 8.5h8M6.5 12h8M6.5 15.5h5"/>',
    "building": '<path d="M5 20.5V5.5l7-2.5 7 2.5v15"/><path d="M3.5 20.5h17"/><path d="M9 9h1.5M13.5 9H15M9 12.5h1.5M13.5 12.5H15M10.5 20.5v-4h3v4"/>',
    "earnings": '<rect x="4" y="3.5" width="16" height="17" rx="2.5"/><path d="M8 16v-3M12 16v-6M16 16v-4.5"/>',
    "bell": '<path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
    "key": '<circle cx="8" cy="15" r="4"/><path d="M11 12l8-8M16 7l2 2M14 9l2 2"/>',
    "search": '<circle cx="10.5" cy="10.5" r="6"/><path d="M15 15l5 5"/>',
    "check": '<circle cx="12" cy="12" r="8.5"/><path d="M8.3 12.2l2.6 2.6 5-5.3"/>',
    "pen": '<path d="M4 20l1-4.5L15.5 5a2.1 2.1 0 0 1 3 3L8 18.5z"/><path d="M13.5 7l3 3"/>',
}


def t(x, y, s, size=18, color=INK, weight=400, anchor="start", italic=False):
    style = ' font-style="italic"' if italic else ""
    return (f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{color}" '
            f'text-anchor="{anchor}"{style}>{escape(s)}</text>')


def icon(name, x, y, size, color, width=1.8):
    k = size / 24
    return (f'<g transform="translate({x},{y}) scale({k:.3f})" fill="none" stroke="{color}" stroke-width="{width / k:.2f}" '
            f'stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</g>')


def logo(file, x, y, h):
    """Embed a PNG from ./logos at height h, keeping its aspect ratio."""
    data = (HERE / "logos" / file).read_bytes()
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    w = h * width / height
    uri = base64.b64encode(data).decode()
    return f'<image x="{x}" y="{y}" width="{w:.1f}" height="{h}" href="data:image/png;base64,{uri}"/>', w


def avatar(x, y, size, fill, ink, badge, badge_fill):
    """A person bust in a rounded tile, with a small badge for the role."""
    s = size
    return (
        f'<rect x="{x}" y="{y}" width="{s}" height="{s}" rx="{s * 0.26}" fill="{fill}"/>'
        f'<circle cx="{x + s * 0.5}" cy="{y + s * 0.4}" r="{s * 0.17}" fill="none" stroke="{ink}" stroke-width="2.4"/>'
        f'<path d="M{x + s * 0.2} {y + s * 0.86}c{s * 0.03}-{s * 0.17} {s * 0.15}-{s * 0.26} {s * 0.3}-{s * 0.26}'
        f's{s * 0.27} {s * 0.09} {s * 0.3} {s * 0.26}" fill="none" stroke="{ink}" stroke-width="2.4" stroke-linecap="round"/>'
        f'<circle cx="{x + s * 0.86}" cy="{y + s * 0.14}" r="{s * 0.2}" fill="{badge_fill}" stroke="#FFFFFF" stroke-width="2"/>'
        + icon(badge, x + s * 0.86 - s * 0.12, y + s * 0.14 - s * 0.12, s * 0.24, "#FFFFFF", 2.2)
    )


def arrow(d, color=LINE, width=2.2):
    mid = {LINE: "g", GREEN: "gr"}[color]
    return (f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}" stroke-linecap="round" '
            f'stroke-linejoin="round" marker-end="url(#{mid})"/>')


def candles(x, y, w, h):
    """Daily candles; the event day is marked."""
    lo = min(b[2] for b in ORCL) - 2
    hi = max(b[1] for b in ORCL) + 2
    step = w / len(ORCL)
    py = lambda v: y + h - (v - lo) / (hi - lo) * h
    out = []
    for i, (o, hi_, lo_, c) in enumerate(ORCL):
        cx = x + step * i + step / 2
        col = "#26A269" if c >= o else "#E5484D"
        out.append(f'<line x1="{cx:.1f}" y1="{py(hi_):.1f}" x2="{cx:.1f}" y2="{py(lo_):.1f}" stroke="{col}" stroke-width="1.2"/>')
        top, bot = py(max(o, c)), py(min(o, c))
        out.append(f'<rect x="{cx - step * 0.36:.1f}" y="{top:.1f}" width="{step * 0.72:.1f}" '
                   f'height="{max(bot - top, 1.6):.1f}" fill="{col}"/>')
    cx = x + step * (len(ORCL) - 0.5)
    out.append(f'<circle cx="{cx:.1f}" cy="{py(ORCL[-1][1]) - 10:.1f}" r="5" fill="#F5A524" stroke="#fff" stroke-width="1.5"/>')
    return "".join(out)


def data_row(y, h, icon_name, label_lines, body):
    out = (
        f'<rect x="40" y="{y}" width="640" height="{h}" rx="20" fill="#FFFFFF" stroke="{CREAM_LINE}" stroke-width="2"/>'
        f'<path d="M60 {y}H170V{y + h}H60a20 20 0 0 1-20-20V{y + 20}a20 20 0 0 1 20-20z" fill="{CREAM}"/>'
        + icon(icon_name, 88, y + (24 if len(label_lines) == 1 else 16), 34, "#3B3A36")
    )
    for i, line in enumerate(label_lines):
        out += t(105, y + h - 24 - 22 * (len(label_lines) - 1 - i), line, 19, "#3B3A36", 500, "middle")
    return out + body


def role_card(x, y, w, h, fill, stroke, av, title, lines):
    out = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="18" fill="{fill}" stroke="{stroke}" stroke-width="1.8"/>'
    out += av
    top = y + h / 2 - 12 * len(lines) - 2
    out += t(x + 104, top + 8, title, 23, INK, 650)
    for i, line in enumerate(lines):
        out += t(x + 104, top + 36 + 23 * i, line, 16, SUB)
    return out


def build() -> str:
    b = []
    rows = [40, 174, 308, 442]
    h = 118

    # ---- Data ----------------------------------------------------------------
    img, _ = logo("yahoo-finance.png", 196, rows[0] + 45, 28)
    b.append(data_row(rows[0], h, "market", ["Market"], img + candles(430, rows[0] + 14, 230, 88)))

    y = rows[1]
    scale = 34
    peers = (
        t(196, y + 47, "ORCL", 17, INK, 600)
        + f'<rect x="300" y="{y + 30}" width="{6.6 * scale:.0f}" height="22" rx="6" fill="{GREEN}"/>'
        + t(300 + 6.6 * scale + 10, y + 47, "+6.6%", 18, GREEN, 700)
        + t(196, y + 87, "Peers", 17, INK, 600)
        + f'<rect x="300" y="{y + 70}" width="{0.6 * scale:.0f}" height="22" rx="6" fill="#A9B4C4"/>'
        + t(300 + 0.6 * scale + 10, y + 87, "+0.6%  (software)", 17, SUB, 500)
    )
    b.append(data_row(y, h, "peers", ["Peers &", "sector"], peers))

    y = rows[2]
    fin, fw = logo("finnhub.png", 196, y + 22, 34)
    bz, _ = logo("benzinga.png", 410, y + 30, 22)
    cnbc, _ = logo("cnbc.png", 196, y + 66, 36)
    news = (fin + t(196 + fw + 8, y + 48, "Finnhub", 22, "#3DAE4B", 700) + bz + cnbc
            + t(410, y + 92, "and publisher sites", 17, SUB, 500))
    b.append(data_row(y, h, "news", ["News"], news))

    y = rows[3]
    seal, _ = logo("sec-seal.png", 196, y + 22, 74)
    filings = seal + t(286, y + 56, "SEC filings", 20, "#12355B", 700) + t(286, y + 80, "official, timestamped", 15, SUB)
    for i, (ic, name) in enumerate([("earnings", "Earnings"), ("bell", "Major events")]):
        cx = 520 + i * 100
        filings += icon(ic, cx - 17, y + 24, 34, "#3B3A36") + t(cx, y + 86, name, 15, SUB, 500, "middle")
    b.append(data_row(y, h, "building", ["Company"], filings))

    mids = [r + h / 2 for r in rows]
    for m in mids:
        b.append(f'<path d="M680 {m}H704" fill="none" stroke="{LINE}" stroke-width="2.2"/>')
    b.append(f'<path d="M704 {mids[0]}V{mids[-1]}" fill="none" stroke="{LINE}" stroke-width="2.2"/>')
    b.append(arrow(f"M704 {(mids[1] + mids[2]) / 2}H738"))

    # ---- AI research desk ------------------------------------------------------
    dx, dy, dw, dh = 748, 40, 400, 520
    b.append(f'<rect x="{dx}" y="{dy}" width="{dw}" height="{dh}" rx="24" fill="#FFF3DE" stroke="#F2B65A" stroke-width="2.5"/>')
    b.append(f'<path d="M{dx} {dy + dh - 58}H{dx + dw}V{dy + dh - 24}a24 24 0 0 1-24 24H{dx + 24}a24 24 0 0 1-24-24z" fill="#F2B65A"/>')
    b.append(t(dx + dw / 2, dy + dh - 20, "AI research desk", 23, "#FFFFFF", 650, "middle"))
    cx, cw = dx + 22, dw - 44
    b.append(role_card(cx, 70, cw, 158, "#EFEAFF", "#A68BFA",
                       avatar(cx + 20, 70 + 41, 68, "#DDD3FF", "#5B3FD1", "search", "#7C5CE6"),
                       "News researcher", ["Reads the news from before", "the move, names a reason and", "quotes the sentence behind it"]))
    b.append(arrow(f"M{dx + dw / 2 - 44} 236V292", LINE, 2))
    b.append(arrow(f"M{dx + dw / 2 + 44} 292V236", LINE, 2))
    b.append(f'<rect x="{dx + dw / 2 - 78}" y="247" width="156" height="34" rx="17" fill="#FFF3DE" stroke="#C8A35C" stroke-width="1.5"/>')
    b.append(t(dx + dw / 2, 270, "second opinion", 16, "#6B5A35", 500, "middle"))
    b.append(role_card(cx, 300, cw, 158, "#EFEAFF", "#A68BFA",
                       avatar(cx + 20, 300 + 41, 68, "#DDD3FF", "#5B3FD1", "check", "#7C5CE6"),
                       "Fact checker", ["Rereads the article and asks:", "does it really explain", "today's move?"]))
    b.append(f'<rect x="{dx}" y="584" width="{dw}" height="54" rx="14" fill="#F6F7F9" stroke="#C7CDD8" stroke-dasharray="6 5"/>')
    b.append(icon("key", dx + 22, 597, 28, SUB))
    b.append(t(dx + 62, 618, "DeepSeek by default, or your own key", 17, SUB, 500))

    # ---- Editor ----------------------------------------------------------------
    ex, ey, ew, eh = 1222, 100, 290, 400
    b.append(arrow(f"M{dx + dw + 6} 300H{ex - 6}"))
    b.append(t((dx + dw + ex) / 2, 287, "reason", 16, SUB, 500, "middle"))
    b.append(t((dx + dw + ex) / 2, 326, "+ quote", 16, SUB, 500, "middle"))
    b.append(f'<rect x="{ex}" y="{ey}" width="{ew}" height="{eh}" rx="20" fill="#F2F6FD" stroke="#7D97C9" stroke-width="2"/>')
    b.append(avatar(ex + 20, ey + 22, 64, "#DCE6F8", "#36589E", "pen", "#4C6FB5"))
    b.append(t(ex + 100, ey + 50, "Editor", 23, INK, 650))
    b.append(t(ex + 100, ey + 76, "fixed rules, no AI", 16, SUB))
    b.append(t(ex + 22, ey + 126, "Publishes only if:", 17, INK, 600))
    rules = ["the quote is in the article", "it came out before the move", "it is about this company", "the stock beat its peers"]
    for i, r in enumerate(rules):
        yy = ey + 168 + i * 50
        b.append(f'<circle cx="{ex + 36}" cy="{yy - 6}" r="12" fill="#E3F5EC"/>')
        b.append(f'<path d="M{ex + 30} {yy - 6}l4 4 7-8" fill="none" stroke="{GREEN}" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>')
        b.append(t(ex + 58, yy, r, 17, INK))
    b.append(t(ex + ew / 2, ey + eh - 22, "otherwise no reason is published", 15, FAINT, 500, "middle", italic=True))

    # ---- Answer ------------------------------------------------------------------
    ax, ay, aw, ah = 1584, 40, 330, 334
    b.append(arrow(f"M{ex + ew + 6} 200H{ax - 6}", GREEN, 2.4))
    b.append(t((ex + ew + ax) / 2, 188, "publish", 16, GREEN, 600, "middle"))
    b.append(f'<rect x="{ax}" y="{ay}" width="{aw}" height="{ah}" rx="20" fill="#F1FBF5" stroke="{GREEN}" stroke-width="2.2"/>')
    b.append(t(ax + 24, ay + 46, "ORCL", 28, INK, 750))
    b.append(t(ax + 112, ay + 46, "+6.6%", 26, GREEN, 750))
    b.append(t(ax + 24, ay + 76, "Dec 19, 2025", 16, SUB))
    b.append(f'<rect x="{ax + aw - 136}" y="{ay + 52}" width="112" height="32" rx="16" fill="{GREEN}"/>')
    b.append(t(ax + aw - 80, ay + 74, "Confirmed", 16, "#FFFFFF", 650, "middle"))
    b.append(f'<rect x="{ax + 24}" y="{ay + 100}" width="4" height="120" rx="2" fill="{GREEN}"/>')
    for i, q in enumerate(["“TikTok signed binding", "agreements to form a new", "U.S. joint venture led", "by Oracle”"]):
        b.append(t(ax + 42, ay + 126 + 28 * i, q, 19, INK, 500, italic=True))
    b.append(t(ax + 24, ay + 256, "coindesk.com, 11:44 UTC", 16, SUB, 500))
    b.append(t(ax + 24, ay + 286, "Peers rose 0.6%, so this was", 16, SUB))
    b.append(t(ax + 24, ay + 308, "about Oracle, not the sector", 16, SUB))

    b.append(t(ax, 404, "When the rules are not met:", 16, FAINT, 600))
    others = [("Reported", "a source names a reason", "#FFF4DB", "#D69A1E"),
              ("With its group", "it moved with its peers", "#E8F0FF", "#4C7BD9"),
              ("No clear cause", "no news explains it", "#F1F3F6", "#8A93A3"),
              ("Ordinary move", "under 2%, nothing to explain", "#F7F7F9", "#B4BAC5")]
    for i, (name, sub, fill, stroke) in enumerate(others):
        yy = 418 + i * 64
        b.append(f'<rect x="{ax}" y="{yy}" width="{aw}" height="54" rx="14" fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>')
        b.append(t(ax + 18, yy + 23, name, 17, INK, 650))
        b.append(t(ax + 18, yy + 44, sub, 15, SUB))

    markers = (
        f'<marker id="g" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 .8L9.5 5L0 9.2z" fill="{LINE}"/></marker>'
        f'<marker id="gr" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 .8L9.5 5L0 9.2z" fill="{GREEN}"/></marker>'
    )
    w, hgt = 1950, 680
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{hgt}" viewBox="0 0 {w} {hgt}" font-family="{FONT}" role="img" aria-labelledby="title desc">
<title id="title">How Catalyst explains a stock move</title>
<desc id="desc">Market prices, peer moves, news and SEC filings go to an AI research desk: a news researcher names a reason and quotes the article, and a fact checker asks whether the article really explains the move. An editor with fixed rules publishes only if the quote is in the article, it came out before the move, it is about this company and the stock beat its peers. Example: Oracle rose 6.6% on December 19, 2025, Confirmed with a quote from coindesk.com while peers rose 0.6%. Otherwise the answer is Reported, With its group, No clear cause or Ordinary move.</desc>
<defs>{markers}</defs>
<rect width="{w}" height="{hgt}" fill="#FFFFFF"/>
{chr(10).join(b)}
</svg>
"""


if __name__ == "__main__":
    OUT.write_text(build())
    print("wrote", OUT, f"{OUT.stat().st_size // 1024} KB")
