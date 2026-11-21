#!/usr/bin/env python3
"""Generate nginx real-IP trust and peer-classification directives."""
import argparse
import ipaddress
import os
import tempfile
from pathlib import Path

MIN_IPV4_RANGES = 10
MIN_IPV6_RANGES = 5


def render(ranges: list[str]) -> str:
    networks = [ipaddress.ip_network(value, strict=False) for value in ranges]
    ipv4 = [network for network in networks if network.version == 4]
    ipv6 = [network for network in networks if network.version == 6]
    if len(ipv4) < MIN_IPV4_RANGES:
        raise ValueError(f"Cloudflare v4 list has {len(ipv4)} ranges; expected at least {MIN_IPV4_RANGES}")
    if len(ipv6) < MIN_IPV6_RANGES:
        raise ValueError(f"Cloudflare v6 list has {len(ipv6)} ranges; expected at least {MIN_IPV6_RANGES}")
    trusted = "".join(f"set_real_ip_from {network};\n" for network in networks)
    peers = "".join(f"    {network} 1;\n" for network in networks)
    return (
        "# Generated from deploy/nginx/cloudflare_ips.txt; include in nginx http context.\n"
        f"{trusted}"
        "geo $realip_remote_addr $catalyst_cloudflare_peer {\n"
        "    default 0;\n"
        f"{peers}"
        "}\n"
    )


def _atomic_write(output: Path, content: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output.parent, delete=False) as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    os.replace(temporary, output)


def generate(source: Path, output: Path) -> None:
    ranges = [line.strip() for line in source.read_text().splitlines()
              if line.strip() and not line.lstrip().startswith("#")]
    _atomic_write(output, render(ranges))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    generate(args.source, args.output)
