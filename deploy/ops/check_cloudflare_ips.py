#!/usr/bin/env python3
"""Compare the checked-in Cloudflare ranges with the official published lists."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

from generate_cloudflare_include import render

DEPLOY = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = DEPLOY / "nginx/cloudflare_ips.txt"
DEFAULT_INCLUDE = DEPLOY / "nginx/catalyst-cloudflare-allow.conf"
URLS = (
    "https://www.cloudflare.com/ips-v4",
    "https://www.cloudflare.com/ips-v6",
)


def _ranges(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def _temporary_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
        return Path(stream.name)


def _read_remote_or_file(url: str, fixture: Path | None) -> str:
    if fixture is not None:
        return fixture.read_text()
    with urlopen(url, timeout=20) as response:  # noqa: S310 - fixed official Cloudflare URLs
        return response.read().decode("ascii")


def check(source: Path, v4: str, v6: str, *, write: bool = False,
          include_output: Path = DEFAULT_INCLUDE) -> int:
    existing_text = source.read_text()
    existing = _ranges(existing_text)
    v4_ranges, v6_ranges = _ranges(v4), _ranges(v6)
    # render() validates each address family and its minimum published count.
    include_content = render(v4_ranges + v6_ranges)
    official = v4_ranges + v6_ranges
    if len(set(official)) != len(official) or not official:
        raise ValueError("official Cloudflare ranges are empty or contain duplicates")
    existing_set, official_set = set(existing), set(official)
    added = [value for value in official if value not in existing_set]
    missing = [value for value in existing if value not in official_set]
    for value in added:
        print(f"ADDED {value}")
    for value in missing:
        print(f"MISSING {value}")
    if not added and not missing and not write:
        print("Cloudflare ranges match the official lists.")
        return 0
    if (added or missing) and not write:
        print("Cloudflare ranges differ; pass --write to update the list and nginx include.", file=sys.stderr)
        return 1

    content = "# Official Cloudflare network ranges (IPv4 + IPv6).\n"
    content += "# SOURCE: https://www.cloudflare.com/ips/\n"
    content += "# MANUAL CHECK REQUIRED before deployment; do not assume this list is current.\n"
    content += "".join(f"{value}\n" for value in official)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    source_backup = source.with_name(f"{source.name}.bak.{stamp}")
    include_backup = include_output.with_name(f"{include_output.name}.bak.{stamp}")
    staged_source = _temporary_text(source, content)
    staged_include = _temporary_text(include_output, include_content)
    try:
        shutil.copy2(source, source_backup)
        if include_output.exists():
            shutil.copy2(include_output, include_backup)
        else:
            include_backup.with_suffix(include_backup.suffix + ".absent").touch()
        os.replace(staged_source, source)
        os.replace(staged_include, include_output)
    finally:
        staged_source.unlink(missing_ok=True)
        staged_include.unlink(missing_ok=True)
    print(f"Updated {source} and generated {include_output}.")
    print(f"Backups: {source_backup}; {include_backup}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--include-output", type=Path, default=DEFAULT_INCLUDE)
    parser.add_argument("--write", action="store_true", help="update the source list and generated nginx include")
    parser.add_argument("--v4-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--v6-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    v4, v6 = (_read_remote_or_file(url, fixture) for url, fixture in zip(URLS, (args.v4_file, args.v6_file)))
    return check(args.source, v4, v6, write=args.write, include_output=args.include_output)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"Cloudflare range check failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
