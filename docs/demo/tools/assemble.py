"""Turn recorded clip frames into 1080p MP4s, a full cut and README GIFs.

usage: python3 assemble.py <workDir> <outDir> [clip ...]
"""
import json
import subprocess
import sys
from pathlib import Path

work, out = Path(sys.argv[1]), Path(sys.argv[2])
only = sys.argv[3:]
out.mkdir(parents=True, exist_ok=True)
ORDER = ["c1-pick-a-day", "c2-live-run", "c2-tail", "c3-confirmed", "c4-other-results", "c5-models-and-keys", "c6-featured-and-data"]
FADE = 0.35


def run(args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def clip_mp4(name: str) -> Path | None:
    folder = work / "clips" / name
    timeline = folder / "timeline.json"
    if not timeline.exists():
        return None
    data = json.loads(timeline.read_text())
    frames, start, end = data["frames"], data["start"], data["end"]
    lines = []
    # Hold the first frame from the clip start, then each frame until the next one arrives.
    times = [max(start, f["t"]) for f in frames]
    times[0] = start
    for i, frame in enumerate(frames):
        nxt = times[i + 1] if i + 1 < len(frames) else end
        lines.append(f"file '{(folder / frame['name']).as_posix()}'\nduration {max(nxt - times[i], 0.001):.4f}")
    lines.append(f"file '{(folder / frames[-1]['name']).as_posix()}'")
    listing = folder / "frames.txt"
    listing.write_text("\n".join(lines) + "\n")
    total = end - start
    target = out / f"{name}.mp4"
    # c2-live-run and c2-tail join without a fade between them.
    fade_in = "" if name == "c2-tail" else f"fade=t=in:st=0:d={FADE},"
    fade_out = "" if name == "c2-live-run" else f"fade=t=out:st={total - FADE:.3f}:d={FADE},"
    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
        "-vf", f"scale=1920:1080:flags=lanczos,fps=30,{fade_in}{fade_out}format=yuv420p",
        "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-movflags", "+faststart", "-t", f"{total:.3f}", str(target),
    ])
    return target


def gif(src: Path, name: str, width: int = 960, fps: int = 12):
    target = out / f"{name}.gif"
    run([
        "ffmpeg", "-y", "-i", str(src), "-vf",
        f"fps={fps},scale={width}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=4",
        str(target),
    ])
    return target


made = []
for name in ORDER:
    if only and name not in only:
        existing = out / f"{name}.mp4"
        if existing.exists():
            made.append(existing)
        continue
    mp4 = clip_mp4(name)
    if mp4:
        made.append(mp4)
        print("clip", mp4.name)

if not only or "full" in only:
    parts = [out / f"{n}.mp4" for n in ORDER if (out / f"{n}.mp4").exists()]
    listing = work / "full.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
    full = out / "catalyst-demo-full.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(full)])
    print("full", full.name)
    for name in ["c1-pick-a-day", "c3-confirmed", "c4-other-results"]:
        if (out / f"{name}.mp4").exists():
            print("gif", gif(out / f"{name}.mp4", name).name)
