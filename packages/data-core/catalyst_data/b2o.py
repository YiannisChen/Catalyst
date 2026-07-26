from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from dotenv import load_dotenv

from catalyst_data.config import RATE_POLICIES, RatePolicy
from catalyst_data.connectors.base import FetchResult
from catalyst_data.coverage_audit import run_b2o_readiness_audit
from catalyst_data.manifests.operations import (
    bootstrap_candidate,
    promote_candidate,
    publish_corpus_with_resource_gate,
    sha256_file,
)
from catalyst_data.manifests.snapshot import build_data_snapshot_manifest
from catalyst_data.manifests.universe import (
    SourceWindows,
    build_b2o_source_scopes,
    build_universe_manifest,
    load_universe_spec,
)
from catalyst_data.rate_limiter import TokenBucketLimiter
from catalyst_data.update_planner import plan_update


@dataclass(frozen=True)
class ProviderCredentials:
    polygon: str | None = None
    finnhub: str | None = None
    fred: str | None = None
    fmp: str | None = None
    sec_user_agent: str | None = None


def _envelope(command: str, status: str, data: dict | None = None, errors: list[str] | None = None) -> dict:
    return {
        "schema_version": "1.0.0",
        "command": command,
        "status": status,
        "data": data or {},
        "errors": errors or [],
    }


def _print(env: dict) -> None:
    print(json.dumps(env, sort_keys=True, separators=(",", ":"), default=str))


class B2OLiveTransport(AbstractAsyncContextManager):
    def __init__(
        self,
        credentials: ProviderCredentials,
        rate_policies: Mapping[str, RatePolicy],
        request_caps: Mapping[str, int],
        clients: Mapping[str, Any] | None = None,
    ):
        self.credentials = credentials
        self._key_ids = {
            name: "set" for name, value in asdict(credentials).items()
            if value and name != "sec_user_agent"
        }
        self.rate_policies = dict(rate_policies)
        self.request_caps = dict(request_caps)
        self._limiters = {
            provider: TokenBucketLimiter(policy)
            for provider, policy in self.rate_policies.items()
        }
        self.clients = dict(clients or {})
        self._owned_clients: dict[str, Any] = {}
        self.closed = False

    def __repr__(self) -> str:
        return "B2OLiveTransport(keys=redacted)"

    async def __aenter__(self) -> "B2OLiveTransport":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        for client in self._owned_clients.values():
            if hasattr(client, "aclose"):
                await client.aclose()
        self.closed = True

    async def retry_sleep(self, delay: float) -> None:
        await asyncio.sleep(delay)

    async def __call__(self, provider: str, method: str, url: str, **kwargs):
        return await self.request(
            provider=provider,
            source_type=kwargs["source"],
            endpoint_name=kwargs["endpoint"],
            subject=kwargs["ticker"],
            window_start=kwargs.get("window_start") or kwargs["date"],
            window_end=kwargs.get("window_end") or kwargs["date"],
        )

    async def request(
        self,
        *,
        provider: str,
        source_type: str,
        endpoint_name: str,
        subject: str,
        window_start: str,
        window_end: str,
        page_url: str | None = None,
        page_cap: int | None = None,
        item_cap: int | None = None,
    ) -> FetchResult:
        if provider == "polygon":
            return await self._request_polygon(source_type, endpoint_name, subject, window_start, window_end, page_url)
        if provider == "finnhub":
            return await self._request_finnhub(endpoint_name, subject, window_start, window_end)
        if provider == "fred":
            return await self._request_fred(endpoint_name, window_start, window_end)
        if provider == "fmp":
            return await self._request_fmp(endpoint_name, subject)
        if provider == "sec":
            return await self._request_sec(endpoint_name, subject)
        raise ValueError(f"unknown B2-O provider: {provider}")

    def _client(self, provider: str):
        if provider in self.clients:
            return self.clients[provider]
        if provider in self._owned_clients:
            return self._owned_clients[provider]
        import httpx
        client = httpx.AsyncClient(timeout=30.0)
        self._owned_clients[provider] = client
        return client

    async def _get(self, provider: str, url: str, **kwargs):
        client = self._client(provider)
        limiter = self._limiters.get(provider)
        if limiter is None:
            return await client.get(url, **kwargs)
        async with limiter.acquire():
            return await client.get(url, **kwargs)

    @staticmethod
    def _result(resp: Any, source_label: str) -> FetchResult:
        raw_body = getattr(resp, "content", None)
        if raw_body is None:
            raw_body = str(getattr(resp, "text", "")).encode("utf-8")
        try:
            data = resp.json()
        except (TypeError, ValueError):
            data = {}
        status = int(resp.status_code)
        error = None
        if status < 200 or status >= 300:
            error = f"{source_label} HTTP {status}: {str(getattr(resp, 'text', ''))[:200]}"
        headers = getattr(resp, "headers", {}) or {}
        retry_after = None
        raw_retry_after = headers.get("retry-after")
        if raw_retry_after is not None:
            try:
                retry_after = float(raw_retry_after)
            except (TypeError, ValueError):
                pass
        return FetchResult(
            status=status,
            data=data,
            error=error,
            source_label=source_label,
            retry_after_seconds=retry_after,
            raw_body=bytes(raw_body),
        )

    async def _request_polygon(self, source_type: str, endpoint: str, subject: str, start: str, end: str, page_url: str | None) -> FetchResult:
        key = self.credentials.polygon
        if not key:
            raise ValueError("missing mandatory provider credential: polygon")
        if page_url:
            parsed = urlparse(page_url)
            if parsed.scheme != "https" or parsed.hostname != "api.polygon.io":
                raise ValueError("untrusted Polygon pagination URL")
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            pairs = [(k, v) for k, v in pairs if k != "apiKey"]
            pairs.append(("apiKey", key))
            url = urlunparse(parsed._replace(query=urlencode(pairs, doseq=True)))
            resp = await self._get("polygon", url)
            return self._result(resp, f"polygon:{endpoint}")
        if endpoint == "ohlcv":
            url = f"https://api.polygon.io/v2/aggs/ticker/{subject}/range/1/day/{start}/{end}"
            params = {"adjusted": "true", "apiKey": key}
        elif endpoint == "news":
            next_day = (datetime.fromisoformat(end) + timedelta(days=1)).strftime("%Y-%m-%d")
            url = "https://api.polygon.io/v2/reference/news"
            params = {
                "ticker": subject,
                "published_utc.gte": f"{start}T00:00:00Z",
                "published_utc.lt": f"{next_day}T00:00:00Z",
                "limit": str(self.request_caps.get(source_type, 50)),
                "apiKey": key,
            }
        else:
            raise ValueError(f"unknown Polygon endpoint: {endpoint}")
        resp = await self._get("polygon", url, params=params)
        return self._result(resp, f"polygon:{endpoint}")

    async def _request_finnhub(self, endpoint: str, subject: str, start: str, end: str) -> FetchResult:
        key = self.credentials.finnhub
        if not key:
            raise ValueError("missing mandatory provider credential: finnhub")
        resp = await self._get(
            "finnhub",
            "https://finnhub.io/api/v1/company-news",
            params={"symbol": subject, "from": start, "to": end, "token": key},
        )
        return self._result(resp, f"finnhub:{endpoint}")

    async def _request_fred(self, endpoint: str, start: str, end: str) -> FetchResult:
        key = self.credentials.fred
        if not key:
            raise ValueError("missing mandatory provider credential: fred")
        resp = await self._get(
            "fred",
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": endpoint, "api_key": key, "file_type": "json"},
        )
        return self._result(resp, f"fred:{endpoint}")

    async def _request_fmp(self, endpoint: str, subject: str) -> FetchResult:
        key = self.credentials.fmp
        if not key:
            raise ValueError("missing provider credential: fmp")
        path = {"income_statement": "income-statement", "balance_sheet": "balance-sheet-statement", "cash_flow": "cash-flow-statement"}[endpoint]
        resp = await self._get(
            "fmp",
            f"https://financialmodelingprep.com/stable/{path}",
            params={"symbol": subject, "period": "annual", "apikey": key},
        )
        return self._result(resp, f"fmp:{endpoint}")

    async def _request_sec(self, endpoint: str, subject: str) -> FetchResult:
        from catalyst_data.cik_map import ticker_to_cik

        user_agent = self.credentials.sec_user_agent
        if not user_agent:
            raise ValueError("missing SEC user agent")
        cik = ticker_to_cik(subject)
        resp = await self._get(
            "sec",
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers={"User-Agent": user_agent},
        )
        return self._result(resp, f"sec:{endpoint}")


def create_b2o_live_transport(
    *,
    credentials: ProviderCredentials | None = None,
    env_path: Path | None = None,
    rate_policies: Mapping[str, RatePolicy],
    request_caps: Mapping[str, int],
    clients: Mapping[str, Any] | None = None,
) -> B2OLiveTransport:
    if env_path is not None and Path(env_path).exists():
        load_dotenv(env_path, override=False)
    if credentials is None:
        credentials = ProviderCredentials(
            polygon=os.environ.get("POLYGON_API_KEY"),
            finnhub=os.environ.get("FINNHUB_API_KEY"),
            fred=os.environ.get("FRED_API_KEY"),
            fmp=os.environ.get("FMP_API_KEY"),
            sec_user_agent=os.environ.get("SEC_USER_AGENT"),
        )
    missing = [
        field
        for field in ("polygon", "finnhub", "fred", "sec_user_agent")
        if not getattr(credentials, field)
    ]
    if missing:
        raise ValueError(f"missing mandatory provider credentials: {', '.join(missing)}")
    return B2OLiveTransport(credentials, rate_policies, request_caps, clients=clients)


def _load_manifest(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def _source_windows_from_manifest(manifest: dict) -> SourceWindows:
    windows = manifest["source_windows"]
    return SourceWindows(**windows)


def _source_windows_from_snapshot(snapshot: dict) -> SourceWindows:
    return SourceWindows(**snapshot["source_windows"])


def _verify_snapshot_manifest_for_db(conn: sqlite3.Connection, snapshot: dict) -> None:
    recomputed = build_data_snapshot_manifest(
        conn,
        universe_manifest_id=snapshot["universe_manifest_id"],
        plan_hash=snapshot["plan_hash"],
        protected_source_sha256=snapshot["protected_source_sha256"],
        source_windows=_source_windows_from_snapshot(snapshot),
        coverage_states=snapshot["coverage_states"],
        created_at=datetime.now(timezone.utc),
    )
    if recomputed.snapshot_id != snapshot["snapshot_id"]:
        raise ValueError("snapshot manifest does not match current DB state")


def _verify_corpus_and_lexical_bound_to_snapshot(conn: sqlite3.Connection, snapshot_id: str) -> None:
    row = conn.execute(
        "SELECT manifest_id, manifest_json FROM corpus_manifest WHERE is_current = 1"
    ).fetchone()
    if row is None:
        raise ValueError("missing current corpus manifest")
    manifest = json.loads(row[1])
    if manifest.get("certified_snapshot_identity") != snapshot_id:
        raise ValueError("current corpus manifest is not bound to snapshot")
    lexical = conn.execute(
        "SELECT corpus_manifest_id FROM lexical_index_state WHERE singleton_id = 1"
    ).fetchone()
    if lexical is None or lexical[0] != row[0]:
        raise ValueError("lexical state is not bound to current corpus")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="catalyst_data.b2o")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("manifest")
    p.add_argument("--spec", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--latest-complete-session", required=True)
    p.add_argument("--discovery-artifact", action="append", default=[])

    p = sub.add_parser("bootstrap")
    p.add_argument("--source-db", required=True)
    p.add_argument("--expected-source-sha256", required=True)
    p.add_argument("--bootstrap-db", required=True)

    p = sub.add_parser("plan")
    p.add_argument("--db", required=True)
    p.add_argument("--universe-manifest", required=True)
    p.add_argument("--latest-complete-session", required=True)
    p.add_argument("--output", required=True)

    p = sub.add_parser("execute")
    p.add_argument("--db", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--expected-plan-hash", required=True)
    p.add_argument("--authorized", action="store_true")
    p.add_argument("--parent-run-id")
    p.add_argument("--max-runtime-seconds", type=float)

    p = sub.add_parser("audit")
    p.add_argument("--db", required=True)
    p.add_argument("--universe-manifest", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--output-dir")
    p.add_argument("--terminal-run-id", required=True)

    p = sub.add_parser("snapshot")
    p.add_argument("--db", required=True)
    p.add_argument("--universe-manifest", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--protected-source-sha256", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--terminal-run-id", required=True)

    p = sub.add_parser("publish-corpus")
    p.add_argument("--db", required=True)
    p.add_argument("--snapshot-manifest", required=True)

    p = sub.add_parser("promote")
    p.add_argument("--db", required=True)
    p.add_argument("--snapshot-manifest", required=True)
    p.add_argument("--snapshots-dir")
    p.add_argument("--active-pointer")

    args = parser.parse_args(argv)
    try:
        if args.command == "manifest":
            spec = load_universe_spec(Path(args.spec))
            windows = SourceWindows.for_latest_complete_session(args.latest_complete_session)
            artifact_hashes = {}
            for artifact in args.discovery_artifact:
                path = Path(artifact)
                artifact_hashes[path.name] = sha256_file(path)
            if not artifact_hashes:
                artifact_hashes = {"offline": "0" * 64}
            manifest = build_universe_manifest(
                spec,
                discovery_artifact_hashes=artifact_hashes,
                observed_provider_capabilities={},
                source_windows=windows,
                coverage_status_summary={},
                approved_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
                created_at=datetime.now(timezone.utc),
            )
            Path(args.output).write_text(json.dumps(manifest.to_dict(), sort_keys=True, separators=(",", ":")))
            _print(_envelope(args.command, "SUCCEEDED", {"runtime_manifest_id": manifest.runtime_manifest_id, "output": str(Path(args.output).resolve())}))
            return 0

        if args.command == "bootstrap":
            result = bootstrap_candidate(
                source_path=Path(args.source_db),
                expected_source_sha256=args.expected_source_sha256,
                candidate_path=Path(args.bootstrap_db),
            )
            _print(_envelope(args.command, "SUCCEEDED", asdict(result)))
            return 0

        if args.command == "plan":
            manifest = _load_manifest(Path(args.universe_manifest))
            spec = load_universe_spec(Path(__file__).resolve().parent / "manifests" / "universe_v1_2025_08.spec.json")
            windows = _source_windows_from_manifest(manifest)
            scopes = build_b2o_source_scopes(spec, windows)
            plan = plan_update(Path(args.db), source_scopes=scopes, reference_today=args.latest_complete_session)
            Path(args.output).write_text(json.dumps(asdict(plan), sort_keys=True, separators=(",", ":"), default=str))
            _print(_envelope(args.command, "SUCCEEDED", {"plan_hash": plan.plan_hash, "cell_counts": plan.estimates.get("requests", {})}))
            return 0

        if args.command == "execute":
            if not args.authorized:
                _print(_envelope(args.command, "FAILED", errors=["execute requires --authorized for B2-O-X"]))
                return 2
            plan_data = json.loads(Path(args.plan).read_text())
            if plan_data.get("plan_hash") != args.expected_plan_hash:
                _print(_envelope(args.command, "FAILED", errors=["expected plan hash mismatch"]))
                return 4
            from catalyst_data.update_planner import UpdatePlan
            from catalyst_data.update_pipeline import execute_update

            plan_obj = UpdatePlan(**plan_data)
            conn = sqlite3.connect(args.db)
            try:
                async def _run_live():
                    async with create_b2o_live_transport(
                        env_path=Path(__file__).resolve().parents[1] / ".env",
                        rate_policies=RATE_POLICIES["dev"],
                        request_caps={},
                    ) as transport:
                        return await execute_update(
                            db=conn,
                            plan=plan_obj,
                            transport=transport,
                            parent_run_id=args.parent_run_id,
                            max_runtime_seconds=args.max_runtime_seconds,
                        )
                report = asyncio.run(_run_live())
            finally:
                conn.close()
            execution_succeeded = report["status"] == "SUCCEEDED"
            _print(_envelope(
                args.command,
                "SUCCEEDED" if execution_succeeded else "FAILED",
                {
                    "run_id": report["run_id"],
                    "status": report["status"],
                    "cells_total": report["cells_total"],
                    "stop_reason": report.get("stop_reason"),
                },
                None if execution_succeeded else ["execution did not complete"],
            ))
            return 0 if execution_succeeded else 6

        if args.command == "audit":
            manifest = _load_manifest(Path(args.universe_manifest))
            plan_obj = None
            if args.plan:
                plan_data = json.loads(Path(args.plan).read_text())
                from catalyst_data.update_planner import UpdatePlan as _UP
                plan_obj = _UP(**plan_data)
            report = run_b2o_readiness_audit(
                args.db,
                universe_manifest=manifest,
                plan=plan_obj,
                output_dir=args.output_dir,
                terminal_run_id=args.terminal_run_id,
            )
            status = report["b2o_readiness"]["comparable_gate"]["status"]
            _print(_envelope(args.command, "SUCCEEDED" if status == "complete" else "FAILED", {"comparable_gate": status}))
            return 0 if status == "complete" else 5

        if args.command == "snapshot":
            manifest = _load_manifest(Path(args.universe_manifest))
            plan_data = json.loads(Path(args.plan).read_text())
            from catalyst_data.update_planner import UpdatePlan as _UP2
            plan_obj = _UP2(**plan_data)
            readiness = run_b2o_readiness_audit(
                args.db,
                universe_manifest=manifest,
                plan=plan_obj,
                terminal_run_id=args.terminal_run_id,
            )
            b2o_readiness = readiness["b2o_readiness"]
            if (
                b2o_readiness["overall_readiness"]["status"] != "complete"
                or b2o_readiness["canonical_news_comparable_gate"]["status"] != "complete"
            ):
                _print(_envelope(args.command, "FAILED", errors=["readiness incomplete"]))
                return 5
            conn = sqlite3.connect(args.db)
            try:
                snap = build_data_snapshot_manifest(
                    conn,
                    universe_manifest_id=manifest["runtime_manifest_id"],
                    plan_hash=plan_data["plan_hash"],
                    protected_source_sha256=args.protected_source_sha256,
                    source_windows=_source_windows_from_manifest(manifest),
                    coverage_states=b2o_readiness,
                    created_at=datetime.now(timezone.utc),
                )
            finally:
                conn.close()
            Path(args.output).write_text(json.dumps(snap.to_dict(), sort_keys=True, separators=(",", ":"), default=str))
            _print(_envelope(args.command, "SUCCEEDED", {"snapshot_id": snap.snapshot_id, "output": str(Path(args.output).resolve())}))
            return 0

        if args.command == "publish-corpus":
            snap = json.loads(Path(args.snapshot_manifest).read_text())
            conn = sqlite3.connect(args.db)
            try:
                _verify_snapshot_manifest_for_db(conn, snap)
                result = publish_corpus_with_resource_gate(
                    conn,
                    snapshot_id=snap["snapshot_id"],
                    clock=lambda: datetime.now(timezone.utc).isoformat(),
                )
            finally:
                conn.close()
            _print(_envelope(args.command, "SUCCEEDED", {"corpus_manifest_id": result.corpus.manifest_id, "lexical_manifest_id": result.lexical.manifest_id}))
            return 0

        if args.command == "promote":
            snap = json.loads(Path(args.snapshot_manifest).read_text())
            conn = sqlite3.connect(args.db)
            try:
                _verify_snapshot_manifest_for_db(conn, snap)
                _verify_corpus_and_lexical_bound_to_snapshot(conn, snap["snapshot_id"])
            finally:
                conn.close()
            result = promote_candidate(
                candidate_path=Path(args.db),
                snapshot_id=snap["snapshot_id"],
                snapshots_dir=Path(args.snapshots_dir) if args.snapshots_dir else Path("data/snapshots"),
                active_pointer_path=Path(args.active_pointer) if args.active_pointer else Path("data/manifests/active_data_snapshot.json"),
            )
            _print(_envelope(args.command, "SUCCEEDED", asdict(result)))
            return 0
    except Exception as exc:
        code = 2
        if args.command in {"bootstrap"}:
            code = 3
        elif args.command in {"promote", "publish-corpus"}:
            code = 7
        _print(_envelope(args.command, "FAILED", errors=[str(exc)]))
        return code
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
