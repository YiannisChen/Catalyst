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
    publish_streaming_corpus_with_resource_gate,
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


def _print_corpus_progress(event: dict[str, Any]) -> None:
    fields = (
        "phase",
        "documents",
        "chunks",
        "source_utf8_bytes",
        "chunk_text_utf8_bytes",
        "elapsed_seconds",
        "rss_bytes",
    )
    safe_event = {key: event[key] for key in fields}
    safe_event["event"] = "corpus_progress"
    print(
        json.dumps(safe_event, sort_keys=True, separators=(",", ":")),
        file=sys.stderr,
    )


def _corpus_publication_payload(result: Any) -> dict[str, Any]:
    return {
        "build_id": result.build_id,
        "corpus_manifest_id": result.corpus.manifest_id,
        "lexical_manifest_id": result.lexical.manifest_id,
        "document_count": result.corpus.document_count,
        "chunk_count": result.corpus.chunk_count,
        "lexical_row_count": result.lexical.row_count,
        "chunk_access": "iter_chunk_pages",
    }


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
        cell: dict | None = None,
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
            return await self._request_sec(
                endpoint_name, subject, cell=cell, page_url=page_url
            )
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

    async def _request_sec(
        self,
        endpoint: str,
        subject: str,
        *,
        cell: dict | None = None,
        page_url: str | None = None,
    ) -> FetchResult:
        from catalyst_data.cik_map import ticker_to_cik
        from catalyst_data.sec.index_parser import filing_index_urls

        user_agent = self.credentials.sec_user_agent
        if not user_agent:
            raise ValueError("missing SEC user agent")
        headers = {"User-Agent": user_agent}
        if endpoint == "sec_submissions":
            cik = ticker_to_cik(subject)
            resp = await self._get(
                "sec",
                f"https://data.sec.gov/submissions/CIK{cik}.json",
                headers=headers,
            )
            return self._result(resp, f"sec:{endpoint}")
        if endpoint == "sec_filing_index":
            # When page_url is set (executor multi-attempt ledger path), fetch exactly that URL.
            if page_url:
                resp = await self._get("sec", page_url, headers=headers)
                return self._result(resp, f"sec:{endpoint}")
            if not cell or not cell.get("identity_extensions"):
                raise ValueError("sec_filing_index requires full SEC v2 cell")
            ext = cell["identity_extensions"]
            urls = filing_index_urls(
                cik_int=str(ext["cik"]), accession=str(ext["accession_number"])
            )
            last: FetchResult | None = None
            for url in urls:
                resp = await self._get("sec", url, headers=headers)
                last = self._result(resp, f"sec:{endpoint}")
                if last.status == 200 and last.raw_body:
                    return last
            return last or FetchResult(status=0, error="index_fetch_failed", source_label="sec")
        if endpoint == "sec_document":
            if page_url:
                url = page_url
            elif cell and cell.get("identity_extensions"):
                url = cell["identity_extensions"]["document_url"]
            else:
                raise ValueError("sec_document requires document_url on cell or page_url")
            resp = await self._get("sec", url, headers=headers)
            return self._result(resp, f"sec:{endpoint}")
        raise ValueError(f"unknown SEC endpoint: {endpoint}")


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


def _pre_b6_gate_is_ready(report: Mapping[str, Any], *, phase: str) -> bool:
    """Select the explicit gate for a Pre-B6 lifecycle phase."""
    if phase == "prebuild":
        return report.get("prebuild_source_ready") is True
    if phase == "postbuild":
        if report.get("postbuild_evidence_ready") is not True:
            return False
        sec_readiness = report.get("sec_readiness") or {}
        return int(sec_readiness.get("chunked_count") or 0) > 0
    raise ValueError(f"unknown Pre-B6 phase: {phase}")


def _load_convergence_plan_hash(evidence_path: str | None) -> str:
    """Always recompute composite hash; never trust stored hash alone."""
    if not evidence_path:
        raise ValueError("Pre-B6 snapshot requires --convergence-evidence")
    from catalyst_data.sec.readiness import load_and_verify_convergence_evidence

    recomputed, _data = load_and_verify_convergence_evidence(evidence_path)
    return recomputed


def _verify_snapshot_manifest_for_db(conn: sqlite3.Connection, snapshot: dict) -> None:
    from catalyst_data.manifests.snapshot import build_legacy_data_snapshot_manifest

    cov = snapshot["coverage_states"]
    if (
        cov.get("sec_source_ready") is True
        or snapshot.get("convergence_plan_hash")
        or (cov.get("readiness_binding") is not None)
    ):
        raise ValueError(
            "Pre-B6 snapshot requires explicit pre-b6 publish/promote verification"
        )
    created_at = datetime.now(timezone.utc)
    recomputed = build_legacy_data_snapshot_manifest(
        conn,
        universe_manifest_id=snapshot["universe_manifest_id"],
        plan_hash=snapshot["plan_hash"],
        protected_source_sha256=snapshot["protected_source_sha256"],
        source_windows=_source_windows_from_snapshot(snapshot),
        coverage_states=cov,
        created_at=created_at,
    )
    if recomputed.snapshot_id != snapshot["snapshot_id"]:
        raise ValueError("snapshot manifest does not match current DB state")


def _rerun_and_verify_pre_b6_snapshot(
    *,
    db_path: str,
    snapshot: dict,
    universe_manifest: dict,
    b2o_plan,
    b2o_terminal_run_id: str,
    baseline_snapshot_id: str,
    filing_inventory_path: str,
    convergence_evidence_path: str,
    s1_terminal_run_id: str,
    s2_terminal_run_id: str,
    s4_terminal_run_id: str,
    expected_inventory_id: str | None,
    s2_index_cell_ids: list[str] | None,
) -> dict:
    """Re-run DB-backed readiness and verify the Pre-B6 snapshot artifact."""
    from catalyst_data.coverage_audit import run_pre_b6_sec_readiness_audit

    readiness = run_pre_b6_sec_readiness_audit(
        db_path,
        universe_manifest=universe_manifest,
        b2o_plan=b2o_plan,
        b2o_terminal_run_id=b2o_terminal_run_id,
        baseline_snapshot_id=baseline_snapshot_id,
        filing_inventory_path=filing_inventory_path,
        convergence_evidence_path=convergence_evidence_path,
        s1_terminal_run_id=s1_terminal_run_id,
        s2_terminal_run_id=s2_terminal_run_id,
        s4_terminal_run_id=s4_terminal_run_id,
        s2_index_cell_ids=s2_index_cell_ids,
        expected_inventory_id=expected_inventory_id,
    )
    if not _pre_b6_gate_is_ready(readiness, phase="prebuild"):
        raise ValueError("current DB-backed Pre-B6 readiness is incomplete")
    if snapshot.get("universe_manifest_id") != universe_manifest.get(
        "runtime_manifest_id"
    ):
        raise ValueError("snapshot universe_manifest_id mismatch")
    expected_binding = readiness["b2o_readiness"].get("readiness_binding")
    stored_binding = (snapshot.get("coverage_states") or {}).get(
        "readiness_binding"
    )
    lifecycle_fields = {"postbuild_evidence_ready"}
    stored_source_binding = {
        key: value
        for key, value in (stored_binding or {}).items()
        if key not in lifecycle_fields
    }
    current_source_binding = {
        key: value
        for key, value in (expected_binding or {}).items()
        if key not in lifecycle_fields
    }
    if stored_source_binding != current_source_binding:
        raise ValueError("snapshot readiness_binding mismatch")
    if readiness.get("convergence_plan_hash") != snapshot.get("plan_hash"):
        raise ValueError("current readiness convergence hash differs from snapshot")
    snapshot_coverage = snapshot.get("coverage_states")
    if not isinstance(snapshot_coverage, dict):
        raise ValueError("snapshot coverage_states missing")
    conn = sqlite3.connect(db_path)
    try:
        recomputed = build_data_snapshot_manifest(
            conn,
            universe_manifest_id=universe_manifest["runtime_manifest_id"],
            plan_hash=snapshot["plan_hash"],
            protected_source_sha256=snapshot["protected_source_sha256"],
            source_windows=_source_windows_from_manifest(universe_manifest),
            coverage_states=snapshot_coverage,
            created_at=datetime.now(timezone.utc),
            convergence_plan_hash=snapshot["plan_hash"],
        )
    finally:
        conn.close()
    if recomputed.snapshot_id != snapshot.get("snapshot_id"):
        raise ValueError("Pre-B6 snapshot does not match current DB/readiness state")
    return readiness


def _verify_corpus_and_lexical_bound_to_snapshot(
    conn: sqlite3.Connection, snapshot_id: str
) -> str:
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
    return str(row[0])


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
    p.add_argument(
        "--filing-inventory",
        help="Frozen FilingInventoryManifest JSON (required for Pre-B6 SEC readiness)",
    )
    p.add_argument("--expected-inventory-id")
    p.add_argument(
        "--pre-b6-sec",
        action="store_true",
        help="Enable Pre-B6 SEC readiness (requires --filing-inventory)",
    )

    p = sub.add_parser("snapshot")
    p.add_argument("--db", required=True)
    p.add_argument("--universe-manifest", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--protected-source-sha256", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--terminal-run-id", required=True)
    p.add_argument(
        "--filing-inventory",
        help="Frozen FilingInventoryManifest JSON (required for Pre-B6 snapshot)",
    )
    p.add_argument("--expected-inventory-id")
    p.add_argument(
        "--convergence-evidence",
        help="JSON with convergence_plan_hash or composite identity components",
    )
    p.add_argument(
        "--pre-b6",
        action="store_true",
        help="Pre-B6 snapshot path (requires convergence evidence + inventory)",
    )

    p = sub.add_parser("publish-corpus")
    p.add_argument("--db", required=True)
    p.add_argument("--snapshot-manifest", required=True)

    p = sub.add_parser("promote")
    p.add_argument("--db", required=True)
    p.add_argument("--snapshot-manifest", required=True)
    p.add_argument("--snapshots-dir")
    p.add_argument("--active-pointer")

    def add_pre_b6_verification_args(command_parser) -> None:
        command_parser.add_argument("--db", required=True)
        command_parser.add_argument("--snapshot-manifest", required=True)
        command_parser.add_argument("--universe-manifest", required=True)
        command_parser.add_argument("--filing-inventory", required=True)
        command_parser.add_argument("--convergence-evidence", required=True)
        command_parser.add_argument("--b2o-plan", required=True)
        command_parser.add_argument("--b2o-terminal-run-id", required=True)
        command_parser.add_argument("--baseline-snapshot-id", required=True)
        command_parser.add_argument("--s1-terminal-run-id", required=True)
        command_parser.add_argument("--s2-terminal-run-id", required=True)
        command_parser.add_argument("--s4-terminal-run-id", required=True)
        command_parser.add_argument("--expected-inventory-id")
        command_parser.add_argument("--s2-index-cell-ids-json")

    p = sub.add_parser("pre-b6-publish-corpus")
    add_pre_b6_verification_args(p)

    p = sub.add_parser("pre-b6-promote")
    add_pre_b6_verification_args(p)
    p.add_argument("--probe-cutoff", required=True)
    p.add_argument("--probe-output", required=True)
    p.add_argument("--snapshots-dir")
    p.add_argument("--active-pointer")

    # Explicit Pre-B6 production commands (never fall back to legacy)
    p = sub.add_parser("pre-b6-audit")
    p.add_argument("--db", required=True)
    p.add_argument("--universe-manifest", required=True)
    p.add_argument("--filing-inventory", required=True)
    p.add_argument("--convergence-evidence", required=True)
    p.add_argument("--b2o-plan", required=True)
    p.add_argument("--b2o-terminal-run-id", required=True)
    p.add_argument("--baseline-snapshot-id", required=True)
    p.add_argument("--s1-terminal-run-id", required=True)
    p.add_argument("--s2-terminal-run-id", required=True)
    p.add_argument("--s4-terminal-run-id", required=True)
    p.add_argument("--expected-inventory-id")
    p.add_argument("--s2-index-cell-ids-json")
    p.add_argument("--output-dir")

    p = sub.add_parser("pre-b6-snapshot")
    p.add_argument("--db", required=True)
    p.add_argument("--universe-manifest", required=True)
    p.add_argument("--filing-inventory", required=True)
    p.add_argument("--convergence-evidence", required=True)
    p.add_argument("--b2o-plan", required=True)
    p.add_argument("--b2o-terminal-run-id", required=True)
    p.add_argument("--baseline-snapshot-id", required=True)
    p.add_argument("--s1-terminal-run-id", required=True)
    p.add_argument("--s2-terminal-run-id", required=True)
    p.add_argument("--s4-terminal-run-id", required=True)
    p.add_argument("--protected-source-sha256", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--expected-inventory-id")
    p.add_argument("--s2-index-cell-ids-json")

    p = sub.add_parser("legacy-snapshot")
    p.add_argument("--db", required=True)
    p.add_argument("--universe-manifest", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--protected-source-sha256", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--terminal-run-id", required=True)

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
            # Legacy B2-O audit only. Pre-B6 must use pre-b6-audit.
            if getattr(args, "pre_b6_sec", False) or getattr(args, "filing_inventory", None):
                _print(_envelope(
                    args.command, "FAILED",
                    errors=["use pre-b6-audit for Pre-B6 SEC readiness; audit is legacy-only"],
                ))
                return 2
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
                pre_b6_sec=False,
            )
            status = report["b2o_readiness"]["comparable_gate"]["status"]
            _print(_envelope(args.command, "SUCCEEDED" if status == "complete" else "FAILED", {"comparable_gate": status}))
            return 0 if status == "complete" else 5

        if args.command == "pre-b6-audit":
            from catalyst_data.coverage_audit import run_pre_b6_sec_readiness_audit
            from catalyst_data.update_planner import UpdatePlan as _PreB6Plan

            manifest = _load_manifest(Path(args.universe_manifest))
            b2o_plan = _PreB6Plan(**json.loads(Path(args.b2o_plan).read_text()))
            index_cells = None
            if getattr(args, "s2_index_cell_ids_json", None):
                index_cells = json.loads(Path(args.s2_index_cell_ids_json).read_text())
            report = run_pre_b6_sec_readiness_audit(
                args.db,
                universe_manifest=manifest,
                b2o_plan=b2o_plan,
                b2o_terminal_run_id=args.b2o_terminal_run_id,
                baseline_snapshot_id=args.baseline_snapshot_id,
                filing_inventory_path=args.filing_inventory,
                convergence_evidence_path=args.convergence_evidence,
                s1_terminal_run_id=args.s1_terminal_run_id,
                s2_terminal_run_id=args.s2_terminal_run_id,
                s4_terminal_run_id=args.s4_terminal_run_id,
                s2_index_cell_ids=index_cells,
                expected_inventory_id=getattr(args, "expected_inventory_id", None),
                output_dir=args.output_dir,
            )
            ok = _pre_b6_gate_is_ready(report, phase="prebuild")
            _errors: list[str] = []
            if not ok:
                _errors.append("prebuild_source_ready_false")
            prov = report["b2o_readiness"].get("required_provenance", {})
            if prov.get("status") != "complete":
                _errors.append(f"mandatory_provenance_incomplete={len(prov.get('missing_or_invalid_cell_ids',[]))}")
            comp = report["b2o_readiness"].get("canonical_news_comparable_gate", {})
            if comp.get("status") != "complete":
                _errors.append(f"comparable_gate_incomplete={len(comp.get('missing_tickers',[]))}")
            _print(_envelope(
                args.command,
                "SUCCEEDED" if ok else "FAILED",
                {
                    "sec_source_ready": report.get("sec_source_ready"),
                    "sec_evidence_ready": report.get("sec_evidence_ready"),
                    "prebuild_source_ready": report.get("prebuild_source_ready"),
                    "postbuild_evidence_ready": report.get("postbuild_evidence_ready"),
                    "convergence_plan_hash": report.get("convergence_plan_hash"),
                    "pre_b6": report.get("pre_b6"),
                    "b2o_overall_status": report["b2o_readiness"]["overall_readiness"]["status"],
                },
                None if ok else _errors,
            ))
            return 0 if ok else 5

        if args.command == "pre-b6-snapshot":
            from catalyst_data.coverage_audit import run_pre_b6_sec_readiness_audit
            from catalyst_data.update_planner import UpdatePlan as _PreB6Plan2

            manifest = _load_manifest(Path(args.universe_manifest))
            b2o_plan = _PreB6Plan2(**json.loads(Path(args.b2o_plan).read_text()))
            index_cells = None
            if getattr(args, "s2_index_cell_ids_json", None):
                index_cells = json.loads(Path(args.s2_index_cell_ids_json).read_text())
            readiness = run_pre_b6_sec_readiness_audit(
                args.db,
                universe_manifest=manifest,
                b2o_plan=b2o_plan,
                b2o_terminal_run_id=args.b2o_terminal_run_id,
                baseline_snapshot_id=args.baseline_snapshot_id,
                filing_inventory_path=args.filing_inventory,
                convergence_evidence_path=args.convergence_evidence,
                s1_terminal_run_id=args.s1_terminal_run_id,
                s2_terminal_run_id=args.s2_terminal_run_id,
                s4_terminal_run_id=args.s4_terminal_run_id,
                s2_index_cell_ids=index_cells,
                expected_inventory_id=getattr(args, "expected_inventory_id", None),
            )
            if not _pre_b6_gate_is_ready(readiness, phase="prebuild"):
                _print(
                    _envelope(
                        args.command,
                        "FAILED",
                        errors=["Pre-B6 B2-O/SEC readiness incomplete"],
                    )
                )
                return 5
            conv_hash = readiness["convergence_plan_hash"]
            coverage_for_snap = {
                **(readiness.get("b2o_readiness") or {}),
                "sec_source_ready": True,
                "sec_evidence_ready": readiness.get("sec_evidence_ready"),
                "sec_readiness": readiness.get("sec_readiness"),
            }
            conn = sqlite3.connect(args.db)
            try:
                snap = build_data_snapshot_manifest(
                    conn,
                    universe_manifest_id=manifest["runtime_manifest_id"],
                    plan_hash=conv_hash,
                    protected_source_sha256=args.protected_source_sha256,
                    source_windows=_source_windows_from_manifest(manifest),
                    coverage_states=coverage_for_snap,
                    created_at=datetime.now(timezone.utc),
                    convergence_plan_hash=conv_hash,
                )
            finally:
                conn.close()
            Path(args.output).write_text(
                json.dumps(snap.to_dict(), sort_keys=True, separators=(",", ":"), default=str)
            )
            _print(_envelope(
                args.command, "SUCCEEDED",
                {
                    "snapshot_id": snap.snapshot_id,
                    "plan_hash": snap.plan_hash,
                    "output": str(Path(args.output).resolve()),
                },
            ))
            return 0

        if args.command in {"snapshot", "legacy-snapshot"}:
            if args.command == "snapshot" and (
                getattr(args, "pre_b6", False)
                or getattr(args, "convergence_evidence", None)
            ):
                _print(_envelope(
                    args.command, "FAILED",
                    errors=["use pre-b6-snapshot for Pre-B6; snapshot is the legacy B2-O entrypoint"],
                ))
                return 2
            from catalyst_data.manifests.snapshot import build_legacy_data_snapshot_manifest
            from catalyst_data.update_planner import UpdatePlan as _UP2

            manifest = _load_manifest(Path(args.universe_manifest))
            plan_data = json.loads(Path(args.plan).read_text())
            plan_obj = _UP2(**plan_data)
            readiness = run_b2o_readiness_audit(
                args.db,
                universe_manifest=manifest,
                plan=plan_obj,
                terminal_run_id=args.terminal_run_id,
                pre_b6_sec=False,
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
                snap = build_legacy_data_snapshot_manifest(
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
            Path(args.output).write_text(
                json.dumps(snap.to_dict(), sort_keys=True, separators=(",", ":"), default=str)
            )
            _print(_envelope(
                args.command, "SUCCEEDED",
                {"snapshot_id": snap.snapshot_id, "output": str(Path(args.output).resolve())},
            ))
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
            _print(
                _envelope(
                    args.command,
                    "SUCCEEDED",
                    {
                        "corpus_manifest_id": result.corpus.manifest_id,
                        "lexical_manifest_id": result.lexical.manifest_id,
                    },
                )
            )
            return 0

        if args.command in {"pre-b6-publish-corpus", "pre-b6-promote"}:
            from catalyst_data.update_planner import UpdatePlan as _VerifyPlan

            snap = json.loads(Path(args.snapshot_manifest).read_text())
            manifest = _load_manifest(Path(args.universe_manifest))
            b2o_plan = _VerifyPlan(**json.loads(Path(args.b2o_plan).read_text()))
            index_cells = None
            if args.s2_index_cell_ids_json:
                index_cells = json.loads(
                    Path(args.s2_index_cell_ids_json).read_text()
                )
            _rerun_and_verify_pre_b6_snapshot(
                db_path=args.db,
                snapshot=snap,
                universe_manifest=manifest,
                b2o_plan=b2o_plan,
                b2o_terminal_run_id=args.b2o_terminal_run_id,
                baseline_snapshot_id=args.baseline_snapshot_id,
                filing_inventory_path=args.filing_inventory,
                convergence_evidence_path=args.convergence_evidence,
                s1_terminal_run_id=args.s1_terminal_run_id,
                s2_terminal_run_id=args.s2_terminal_run_id,
                s4_terminal_run_id=args.s4_terminal_run_id,
                expected_inventory_id=args.expected_inventory_id,
                s2_index_cell_ids=index_cells,
            )
            if args.command == "pre-b6-publish-corpus":
                conn = sqlite3.connect(args.db)
                try:
                    result = publish_streaming_corpus_with_resource_gate(
                        conn,
                        snapshot_id=snap["snapshot_id"],
                        clock=lambda: datetime.now(timezone.utc).isoformat(),
                        progress_callback=_print_corpus_progress,
                    )
                finally:
                    conn.close()
                _print(
                    _envelope(
                        args.command,
                        "SUCCEEDED",
                        _corpus_publication_payload(result),
                    )
                )
                return 0

            conn = sqlite3.connect(args.db)
            try:
                corpus_manifest_id = _verify_corpus_and_lexical_bound_to_snapshot(
                    conn, snap["snapshot_id"]
                )
            finally:
                conn.close()
            from catalyst_data.coverage_audit import (
                run_pre_b6_sec_readiness_audit as _post_corpus_readiness,
            )

            evidence_readiness = _post_corpus_readiness(
                args.db,
                universe_manifest=manifest,
                b2o_plan=b2o_plan,
                b2o_terminal_run_id=args.b2o_terminal_run_id,
                baseline_snapshot_id=args.baseline_snapshot_id,
                filing_inventory_path=args.filing_inventory,
                convergence_evidence_path=args.convergence_evidence,
                s1_terminal_run_id=args.s1_terminal_run_id,
                s2_terminal_run_id=args.s2_terminal_run_id,
                s4_terminal_run_id=args.s4_terminal_run_id,
                s2_index_cell_ids=index_cells,
                expected_inventory_id=args.expected_inventory_id,
                corpus_manifest_id=corpus_manifest_id,
                output_dir=str(Path(args.probe_output).parent),
            )
            if not _pre_b6_gate_is_ready(evidence_readiness, phase="postbuild"):
                chunked_count = int(
                    (evidence_readiness.get("sec_readiness") or {}).get(
                        "chunked_count"
                    )
                    or 0
                )
                if chunked_count == 0:
                    raise ValueError(
                        "Pre-B6 promotion requires postbuild_evidence_ready=true; "
                        "chunked_count=0"
                    )
                raise ValueError(
                    "Pre-B6 promotion requires postbuild_evidence_ready=true"
                )
            if evidence_readiness.get("convergence_plan_hash") != snap.get(
                "plan_hash"
            ):
                raise ValueError(
                    "post-corpus readiness convergence hash differs from snapshot"
                )
            from catalyst_data.pre_b6_probes import run_pre_b6_probe_gates

            conn = sqlite3.connect(args.db)
            try:
                probe_report = run_pre_b6_probe_gates(
                    conn,
                    universe_manifest_id=manifest["runtime_manifest_id"],
                    snapshot_id=snap["snapshot_id"],
                    corpus_manifest_id=corpus_manifest_id,
                    ordered_tickers=list(manifest.get("tickers") or []),
                    probe_cutoff=args.probe_cutoff,
                    output_path=Path(args.probe_output),
                    postbuild_readiness_report_path=Path(
                        evidence_readiness["report_path"]
                    ),
                )
            finally:
                conn.close()
            result = promote_candidate(
                candidate_path=Path(args.db),
                snapshot_id=snap["snapshot_id"],
                snapshots_dir=(
                    Path(args.snapshots_dir)
                    if args.snapshots_dir
                    else Path("data/snapshots")
                ),
                active_pointer_path=(
                    Path(args.active_pointer)
                    if args.active_pointer
                    else Path("data/manifests/active_data_snapshot.json")
                ),
            )
            _print(
                _envelope(
                    args.command,
                    "SUCCEEDED",
                    {
                        **asdict(result),
                        "corpus_manifest_id": corpus_manifest_id,
                        "probe_report_id": probe_report["probe_report_id"],
                        "probe_report_path": str(Path(args.probe_output).resolve()),
                    },
                )
            )
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
        failure_data: dict[str, Any] = {}
        if args.command in {"bootstrap"}:
            code = 3
        elif args.command in {
            "promote",
            "publish-corpus",
            "pre-b6-promote",
            "pre-b6-publish-corpus",
        }:
            code = 7
        probe_report = getattr(exc, "report", None)
        if isinstance(probe_report, dict) and probe_report.get("probe_report_id"):
            failure_data = {
                "probe_report_id": probe_report["probe_report_id"],
                "probe_report_path": str(
                    Path(getattr(args, "probe_output")).resolve()
                ),
            }
        _print(
            _envelope(
                args.command,
                "FAILED",
                data=failure_data,
                errors=[str(exc)],
            )
        )
        return code
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
