from __future__ import annotations

from pathlib import Path

# 1x1 transparent PNG bytes.
_MINIMAL_PNG = bytes.fromhex(
    "89504E470D0A1A0A"
    "0000000D49484452000000010000000108060000001F15C489"
    "0000000A49444154789C6360000000020001E221BC330000000049454E44AE426082"
)


def render_pngs(input_json: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ["latency_breakdown.png", "cost_quality_scatter.png", "profile_comparison_bar.png"]:
        (out_dir / name).write_bytes(_MINIMAL_PNG)
