"""The source registry (data/manifests/sources_v1.json): what is collected, how often, into which store, by which
timer, and how loudly a failure is reported. Collectors skip a source that is not enabled or on trial, the deploy
script enables only the timers the registry names, and the health report hands the registry to the monitor, so the
three never drift apart (packages/data/tests/ingestion/test_sources.py).

    python -m catalyst_data.ingestion.sources timers     # the timers to enable, one per line
    python -m catalyst_data.ingestion.sources check      # the registry against deploy unit files; exit 1 on a mismatch
"""
from __future__ import annotations

import json
import re
import sys
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
REGISTRY = REPO / "data/manifests/sources_v1.json"
DEPLOY = REPO / "deploy"
ACTIVE = ("enabled", "trial")
STATUSES = ("enabled", "trial", "disabled", "planned")


@lru_cache(maxsize=4)
def load(path: str | None = None) -> dict:
    return json.loads(Path(path or REGISTRY).read_text())


def find(store: str, key: str, registry: dict | None = None) -> dict | None:
    """The registry entry for a health-report key in a store (exact key first, then patterns)."""
    entries = [s for s in (registry or load())["sources"] if s["store"] == store]
    return next((s for s in entries if s["key"] == key), None) or next(
        (s for s in entries if fnmatchcase(key, s["key"])), None)


def active(source_id: str, registry: dict | None = None) -> bool:
    """True when the source is enabled or on trial. An id the registry does not know is not active."""
    entry = next((s for s in (registry or load())["sources"] if s["id"] == source_id), None)
    return entry is not None and entry["status"] in ACTIVE


def timers(registry: dict | None = None) -> list[str]:
    reg = registry or load()
    names = {t for s in reg["sources"] if s["status"] in ACTIVE for t in s["timers"]}
    return sorted(names | set(reg.get("infrastructure_timers", {})))


def unit_paths(deploy: Path, pattern: str) -> list[Path]:
    """Unit files in deploy/ or its api/, collect/, and ops/ directories."""
    found: list[Path] = []
    for root in (deploy, deploy / "api", deploy / "collect", deploy / "ops"):
        if root.is_dir():
            found.extend(root.glob(pattern))
    return sorted(found)


def unit_sources(deploy: Path = DEPLOY) -> dict[str, set[str]]:
    """{unit name: the news_archive sources its ExecStart polls} for the collect units that take --sources."""
    out = {}
    for f in unit_paths(deploy, "catalyst-collect-*.service"):
        m = re.search(r"--sources\s+(\S+)", f.read_text())
        if m:
            out[f.name[: -len(".service")]] = set(m.group(1).split(","))
    return out


def problems(registry: dict | None = None, deploy: Path = DEPLOY) -> list[str]:
    """Mismatches between the registry and the systemd units in deploy/."""
    reg = registry or load()
    out = []
    ids = [s["id"] for s in reg["sources"]]
    out += [f"duplicate id {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    for s in reg["sources"]:
        if s["status"] not in STATUSES:
            out.append(f"{s['id']}: unknown status {s['status']}")
        if s["store"] not in reg["stores"]:
            out.append(f"{s['id']}: unknown store {s['store']}")
        if s.get("tier") not in (1, 2, 3):
            out.append(f"{s['id']}: tier must be 1, 2 or 3")
        if s["status"] in ACTIVE and s["expected_hours"] is not None and not s["timers"]:
            out.append(f"{s['id']}: expected every {s['expected_hours']} h but no timer runs it")
        if s["status"] not in ACTIVE and s["timers"]:
            out.append(f"{s['id']}: {s['status']} but still names timers {s['timers']}")
    files = {f.name[: -len(".timer")] for f in unit_paths(deploy, "catalyst-*.timer")}
    named = set(timers(reg))
    out += [f"timer {t} named in the registry has no unit file in deploy/" for t in sorted(named - files)]
    collect = {t for t in files if t.startswith("catalyst-collect-")}
    out += [f"timer {t} in deploy/ runs no active registry source" for t in sorted(collect - named)]
    for unit, polled in unit_sources(deploy).items():
        for key in sorted(polled):
            entry = find("news_archive", key, reg)
            if entry is None or entry["status"] not in ACTIVE:
                out.append(f"{unit} polls {key}, which is {entry['status'] if entry else 'not registered'}")
            elif unit not in entry["timers"]:
                out.append(f"{unit} polls {key}, but the registry names {entry['timers']}")
    return out


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "check"
    if cmd == "timers":
        print("\n".join(timers()))
        return 0
    found = problems()
    print("\n".join(found) or "registry consistent with deploy/")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
