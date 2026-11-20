#!/usr/bin/env python3
"""Fail-closed comparison of pinned SQLite snapshots over an explicit UTC window.

No live stores are opened. A pinned acceptance plan declares source IDs, snapshot
paths, and actual polling cadence. A JSON run journal must record every source on
both sides; the old timestamp-only catalyst-shadow journal is unverifiable.
Equal item sets are a conservative acceptance requirement: additions are reported
separately, but require review rather than automatically authorizing a switch.
Run instrumentation and timer changes are separate deployment prerequisites.
The technical gate covers the scheduled Alpaca news source only; it never approves deployment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from catalyst_data.ingestion import input_manifest
from catalyst_data.ingestion.time import timefields

SIDES = ("production", "shadow")
SUPPORTED = {"alpaca_news"}


def stamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be UTC and timezone-aware")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("timestamp must be UTC and timezone-aware") from None
    if result.tzinfo is None or result.utcoffset().total_seconds() != 0:
        raise ValueError("timestamp must be UTC and timezone-aware")
    return result


def canonical_url(value):
    if not value:
        raise ValueError("missing stable article ID and URL")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("invalid article URL")
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if not k.lower().startswith("utm_")]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/",
                       urlencode(sorted(query)), ""))


def digest(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid text field")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_source(path, source, start, end):
    path = Path(path)
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValueError("missing or nonabsolute snapshot")
    if Path(str(path) + "-wal").exists() and Path(str(path) + "-wal").stat().st_size:
        raise ValueError("snapshot has live WAL")
    # immutable is appropriate only because the manifest verifies operator-created snapshots.
    with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        ledger = []
        for row in conn.execute("SELECT subject, attempted_at, status FROM intake_log"):
            subject = row["subject"]
            if not isinstance(subject, str):
                raise ValueError("invalid ledger subject")
            key = subject
            if not fnmatchcase(key, source["key"]):
                continue
            when = stamp(row["attempted_at"])
            if start <= when < end:
                if row["status"] not in ("OK", "EMPTY", "ERROR", "DISABLED"):
                    raise ValueError("invalid ledger status")
                ledger.append(row["status"])
        store = source["store"]
        rows = conn.execute("SELECT id AS stable_id, url, created_at AS published_at, updated_at, "
                            "headline AS title, content AS body, NULL AS publisher, first_seen_at FROM news")
        items = {}
        for row in rows:
            seen = stamp(row["first_seen_at"])
            if seen >= end:
                continue
            if not row["published_at"]:
                raise ValueError("undated observed row cannot be placed in publication window")
            published_time = stamp(row["published_at"])
            if not start <= published_time < end:
                continue
            stable = row["stable_id"]
            identity = "id:" + str(stable) if stable not in (None, "") else "url:" + canonical_url(row["url"])
            if identity in items:
                raise ValueError("duplicate stable item identity")
            published = stamp(row["published_at"]).isoformat() if row["published_at"] else None
            updated = stamp(row["updated_at"]).isoformat() if row["updated_at"] else None
            items[identity] = {"published_at": published, "provider_updated_at": updated,
                               "title_sha256": digest(row["title"]), "body_sha256": digest(row["body"])}
    return ledger, items


def run_problems(rows, side, source_id, cadence, start, end, expected_revision):
    selected = [r for r in rows if r.get("side") == side and r.get("source_id") == source_id]
    times, revisions, errors = [], set(), []
    for run in selected:
        begin, finish = stamp(run["started_at"]), stamp(run["finished_at"])
        if finish < begin:
            raise ValueError("run finish precedes start")
        if not start <= begin < end:
            continue
        if finish > end:
            errors.append("run finishes after common window")
        revision = run.get("revision", "")
        if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
            errors.append("run lacks fixed commit SHA")
        revisions.add(revision)
        if revision != expected_revision:
            errors.append("run revision does not match pinned acceptance plan")
        if run.get("status") not in ("OK", "EMPTY"):
            errors.append("run not completed successfully")
        if run.get("cadence_seconds") != cadence:
            errors.append("run cadence does not match acceptance plan")
        times.append(begin)
    if len(revisions) != 1:
        errors.append("missing or changed revision during window")
    times.sort()
    # One cadence at each edge accommodates a poll phase offset. Interior gaps
    # exceeding 1.5 intervals reject missed polls rather than assuming they ran.
    if not times or (times[0] - start).total_seconds() > cadence or (end - times[-1]).total_seconds() > cadence:
        errors.append("run journal does not cover common window")
    if any((b - a).total_seconds() > cadence * 1.5 for a, b in zip(times, times[1:])):
        errors.append("run journal has missing polling intervals")
    if len(times) < math.floor((end - start).total_seconds() / cadence):
        errors.append("insufficient source runs")
    if len(set(times)) != len(times):
        errors.append("duplicate source runs")
    return sorted(set(errors))


def compare(registry, plan, runs, *, start, end, now):
    start_time, end_time, now_time = map(stamp, (start, end, now))
    if (end_time - start_time).total_seconds() < 24 * 3600:
        raise ValueError("common window must span at least 24 hours")
    if end_time > now_time:
        raise ValueError("common window has not elapsed")
    if not isinstance(runs, dict) or runs.get("schema_version") != 1 or not isinstance(runs.get("runs"), list):
        raise ValueError("runs must be a verifiable schema_version 1 JSON journal; legacy runs rejected")
    if any(not isinstance(r, dict) for r in runs["runs"]):
        raise ValueError("invalid runs record")
    if plan.get("schema_version") != 1 or not isinstance(plan.get("sources"), list) or not plan["sources"]:
        raise ValueError("acceptance plan requires explicit source IDs")
    scheduled = {s["id"]: s for s in registry["sources"]
                 if s["status"] in ("enabled", "trial") and s.get("timers") and s["store"] != "daily"}
    # Source category, not adapter availability, determines gate membership.
    # Future unsupported news stores must remain mandatory news coverage.
    active = {source_id: source for source_id, source in scheduled.items()
              if source.get("category") in ("news", "press_release")}
    if len({s["id"] for s in registry["sources"]}) != len(registry["sources"]):
        raise ValueError("duplicate source IDs in registry")
    selected = [s["id"] for s in plan["sources"]]
    if len(set(selected)) != len(selected):
        raise ValueError("duplicate source IDs in acceptance plan")
    if plan.get("timestamp_policy") != "stored_utc_timefields_v1":
        raise ValueError("acceptance plan requires explicit stored_utc_timefields_v1 timestamp_policy")
    for side in SIDES:
        revision = plan["revisions"][side]
        if not isinstance(revision, str) or len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
            raise ValueError("acceptance plan requires fixed immutable revisions")
        captured = stamp(plan["snapshot_captured_at"][side])
        if not end_time <= captured <= now_time:
            raise ValueError("snapshot capture provenance must be at or after window end and not in future")
    out = {"timestamp_policy": plan["timestamp_policy"], "revisions": plan["revisions"], "snapshot_captured_at": plan["snapshot_captured_at"], "window": {"start": start, "end": end, "interval": "[start,end)"}, "sources": {},
           "comparison_kind": "news_only",
           "uncovered_news_sources": sorted(set(active) - set(selected)),
           "scope_ready": True, "news_comparison_ready": False,
           "deployment_approval": "not_assessed_requires_owner_confirmation",
           "item_scope": "publication in [start,end), first_seen before end; latest pinned snapshot values",
           "limitations": ["older-publication articles and their historical body updates are outside this window",
                           "snapshot comparison is not a complete audit of every historical article returned by pollers",
                           "snapshot captured_at is operator-declared provenance bound by the pinned acceptance plan; not independent attestation",
                           "comparison does not attest to provider completeness"]}
    for declaration in plan["sources"]:
        source_id = declaration["id"]
        result = {"errors": [], "matched_items": 0, "field_mismatches": [], "production_only": [], "shadow_only": []}
        out["sources"][source_id] = result
        source = active.get(source_id)
        if not source or source["store"] not in SUPPORTED:
            result["errors"].append("source inactive, unscheduled, outside news scope or unsupported by comparison adapter")
            out["scope_ready"] = False
            continue
        p_cadence, s_cadence = (declaration.get(k) for k in ("production_cadence_seconds", "shadow_cadence_seconds"))
        if any(isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c) or c <= 0 for c in (p_cadence, s_cadence)):
            result["errors"].append("positive polling cadence required")
            out["scope_ready"] = False
            continue
        expected = source.get("expected_hours")
        if p_cadence != s_cadence or (expected is not None and abs(p_cadence - expected * 3600) > 1):
            result["errors"].append("polling schedules differ; align actual schedules and restart a fresh 24-hour window")
        sides = {}
        for side, cadence in (("production", p_cadence), ("shadow", s_cadence)):
            try:
                result["errors"] += [side + ": " + e for e in run_problems(runs["runs"], side, source_id, cadence, start_time, end_time, plan["revisions"][side])]
                path = plan["snapshots"][side][source["store"]]
                ledger, items = read_source(path, source, start_time, end_time)
                result["body_representation"] = "full_text"
                result[side] = {"ledger_status_counts": {s: ledger.count(s) for s in sorted(set(ledger))}, "items": len(items)}
                if not ledger or not items:
                    result["errors"].append(side + ": missing ledger or item data; EMPTY alone cannot certify item parity")
                if any(s in ("ERROR", "DISABLED") for s in ledger):
                    result["errors"].append(side + ": ledger contains unsuccessful attempts; review required")
                sides[side] = items
            except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
                result["errors"].append(side + ": snapshot, schema, run journal or row validation failed")
        if len(sides) == 2:
            p_items, s_items = sides["production"], sides["shadow"]
            shared = p_items.keys() & s_items.keys()
            result["matched_items"] = len(shared)
            result["production_only"] = sorted(p_items.keys() - s_items.keys())
            result["shadow_only"] = sorted(s_items.keys() - p_items.keys())
            for identity in sorted(shared):
                fields = [k for k in p_items[identity] if p_items[identity][k] != s_items[identity][k]]
                if fields:
                    result["field_mismatches"].append({"identity": identity, "fields": fields})
            if result["production_only"] or result["shadow_only"] or result["field_mismatches"]:
                result["errors"].append("item differences require review; shadow additions are not classified as degradation")
        if result["errors"]:
            out["scope_ready"] = False
    out["news_comparison_ready"] = out["scope_ready"] and not out["uncovered_news_sources"]
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("registry", "plan", "runs", "start", "end"):
        parser.add_argument("--" + option, required=True)
    args = parser.parse_args(argv)
    invocation = [str(Path(__file__).resolve()), *(sys.argv[1:] if argv is None else argv)]
    try:
        manifest = os.environ.get("CATALYST_INPUT_MANIFEST")
        if not manifest:
            raise input_manifest.PreflightError("CATALYST_INPUT_MANIFEST is required")
        base_paths = [args.registry, args.plan, args.runs, Path(__file__).resolve(), Path(input_manifest.__file__).resolve(), Path(timefields.__file__).resolve()]
        required_env = ["PYTHONPATH", "CATALYST_ARCHIVE_DIR", "CATALYST_DATA_DIR", "CATALYST_RATELIMIT_DIR", "CATALYST_RESILIENCE_DB"]
        # Verify the plan before reading it; then bind every referenced database
        # before opening any snapshot. Emit one complete receipt, first in output.
        input_manifest.verify(manifest, invocation=invocation, required_paths=base_paths, required_env=required_env)
        plan = json.loads(Path(args.plan).read_text())
        snapshots = [path for stores in plan["snapshots"].values() for path in stores.values()]
        input_manifest.require_manifest(invocation=invocation, required_paths=[*base_paths, *snapshots], required_env=required_env)
        registry = json.loads(Path(args.registry).read_text())
        runs = json.loads(Path(args.runs).read_text())
        result = compare(registry, plan, runs, start=args.start, end=args.end, now=datetime.now(timezone.utc).isoformat())
    except input_manifest.PreflightError as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}))
        return 2
    except (ValueError, OSError, KeyError, TypeError):
        print(json.dumps({"status": "ERROR", "error": "invalid comparison configuration or unverifiable runs"}))
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["news_comparison_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
