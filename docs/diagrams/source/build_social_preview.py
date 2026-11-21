"""Generate docs/diagrams/social-preview.svg (1280x640) for the GitHub social preview.

    python docs/diagrams/source/build_social_preview.py

Render it to PNG at 1280x640 before uploading (GitHub accepts PNG/JPG, not SVG).
It reuses the framework figure's palette and the real ORCL 2025-12-19 case.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_framework import FONT, GREEN, INK, ORCL, RED, SUB, t  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "social-preview.svg"
AMBER = "#F2A93B"


def mark(x, y, size):
    k = size / 24
    return (
        f'<g transform="translate({x},{y}) scale({k:.3f})">'
        f'<rect width="24" height="24" rx="6" fill="{AMBER}"/>'
        '<path d="M4.5 16.5H9.5L14.5 7.5H19.5" fill="none" stroke="#1A1206" stroke-width="2.4" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        '<circle cx="9.5" cy="16.5" r="2.4" fill="#1A1206"/></g>'
    )


def candles(x, y, w, h):
    bars = ORCL[-16:]
    lo = min(b[2] for b in bars) - 2
    hi = max(b[1] for b in bars) + 2
    step = w / len(bars)
    py = lambda v: y + h - (v - lo) / (hi - lo) * h
    out = []
    for i, (o, hi_, lo_, c) in enumerate(bars):
        cx = x + step * i + step / 2
        col = "#26A269" if c >= o else RED
        out.append(f'<line x1="{cx:.1f}" y1="{py(hi_):.1f}" x2="{cx:.1f}" y2="{py(lo_):.1f}" stroke="{col}" stroke-width="1.6"/>')
        top, bot = py(max(o, c)), py(min(o, c))
        out.append(f'<rect x="{cx - step * 0.34:.1f}" y="{top:.1f}" width="{step * 0.68:.1f}" '
                   f'height="{max(bot - top, 2):.1f}" rx="1" fill="{col}"/>')
    cx = x + step * (len(bars) - 0.5)
    # The event day: a soft band behind its candle and the move above it.
    out.insert(0, f'<rect x="{cx - step * 0.62:.1f}" y="{y - 30}" width="{step * 1.24:.1f}" height="{h + 34}" rx="8" '
                  f'fill="#F5A524" fill-opacity=".16"/>')
    out.append(f'<circle cx="{cx:.1f}" cy="{py(bars[-1][1]) - 12:.1f}" r="6" fill="#F5A524" stroke="#fff" stroke-width="2"/>')
    out.append(t(cx - step * 0.8, y - 8, "+6.6% on the news", 16, GREEN, 700, "end"))
    return "".join(out)


def pill(x, y, text, fill, stroke, color=INK):
    w = 22 + len(text) * 10.2
    return (f'<rect x="{x}" y="{y}" width="{w:.0f}" height="40" rx="20" fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>'
            + t(x + w / 2, y + 27, text, 18, color, 600, "middle")), w


def build() -> str:
    b = []
    # Left: identity and claim.
    b.append(mark(72, 92, 64))
    b.append(t(152, 140, "Catalyst", 52, INK, 750))
    b.append(t(72, 252, "Why did this", 54, INK, 750))
    b.append(t(72, 316, "stock move?", 54, INK, 750))
    b.append(t(72, 374, "The cause, in the source's own words,", 25, SUB, 500))
    b.append(t(72, 408, "from the news and filings behind the move.", 25, SUB, 500))
    x = 72
    for text, fill, stroke in (("News + SEC filings", "#FFF3DE", "#F2B65A"),
                               ("Peers and sector", "#E8F0FF", "#7D97C9"),
                               ("Open source · MIT", "#F1F3F6", "#B4BAC5")):
        svg, w = pill(x, 470, text, fill, stroke)
        b.append(svg)
        x += w + 14
    b.append(t(72, 572, "catalyst.yiannischen.dev", 22, "#8A93A3", 600))

    # Right: one real answer card.
    cx, cy, cw, ch = 716, 72, 492, 496
    b.append(f'<rect x="{cx + 10}" y="{cy + 14}" width="{cw}" height="{ch}" rx="28" fill="#E9E3D6"/>')
    b.append(f'<rect x="{cx}" y="{cy}" width="{cw}" height="{ch}" rx="28" fill="#FFFFFF" stroke="#E6DCC8" stroke-width="2"/>')
    b.append(t(cx + 32, cy + 62, "ORCL", 36, INK, 750))
    b.append(t(cx + 148, cy + 62, "+6.6%", 34, GREEN, 750))
    b.append(t(cx + 32, cy + 94, "Fri, Dec 19, 2025", 18, SUB, 500))
    b.append(f'<rect x="{cx + cw - 172}" y="{cy + 34}" width="140" height="40" rx="20" fill="{GREEN}"/>')
    b.append(t(cx + cw - 102, cy + 61, "Confirmed", 19, "#FFFFFF", 700, "middle"))
    b.append(candles(cx + 32, cy + 142, cw - 64, 104))
    b.append(f'<line x1="{cx + 32}" y1="{cy + 270}" x2="{cx + cw - 32}" y2="{cy + 270}" stroke="#EEE7DA" stroke-width="2"/>')
    b.append(f'<rect x="{cx + 32}" y="{cy + 296}" width="5" height="104" rx="2.5" fill="{GREEN}"/>')
    for i, line in enumerate(["“TikTok signed binding agreements", "to form a new U.S. joint venture", "led by Oracle”"]):
        b.append(t(cx + 54, cy + 324 + 34 * i, line, 23, INK, 500, italic=True))
    b.append(t(cx + 32, cy + 450, "coindesk.com · 6:44 AM ET · peers +0.6%", 18, SUB, 500))

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="640" viewBox="0 0 1280 640" font-family="{FONT}" role="img" aria-label="Catalyst: why did this stock move? The cause in the source's own words, from the news and filings behind the move.">
<rect width="1280" height="640" fill="#FBF7EF"/>
{chr(10).join(b)}
</svg>
"""


if __name__ == "__main__":
    OUT.write_text(build())
    print("wrote", OUT)
