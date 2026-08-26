"""M3-12A/B operator promotion CLI (exact subcommand contract).

Sole promotion CLI: ``prepare`` / ``promote`` / ``rollback`` with required
explicit paths. No ``--force``. ``--dry-run`` exists only on ``prepare`` and
``promote`` and never mutates pointers, journal, or ``active_generation.json``.
Every path is required, absolute, and resolved/rejected for aliasing
(symlink/hardlink/samefile) to the frozen or live DB. ``--expected-
implementation-head`` must equal the current worktree HEAD before any mutation.

GPU work is never performed inside this CLI; ``promote`` consumes a separately
produced GPU artifact. Secrets, credentials, and uncontrolled filesystem paths
never appear in prompts, logs, reports, or manifests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
JOURNAL_DEFAULT = REPO_ROOT / "data" / "manifests" / "m3_promotion_journal.json"
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")

# Exact frozen source snapshot (supervisor-locked). Always in the alias-rejection
# set so a derivative can never be a symlink/hardlink/samefile of the frozen DB,
# even when the worktree has no data/manifests/active_data_snapshot.json.
FROZEN_SNAPSHOT_PATH = Path(
    "/Users/yiannischen/Projects/Catalyst/data/snapshots/"
    "catalyst_b2o_7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49.db"
)

from catalyst_data.index.v1_promote import (  # noqa: E402
    PromotionResult,
    promote_v1_generation,
    rollback_v1_generation,
)
from catalyst_data.index.v1_staging import stage_dense  # noqa: E402
from catalyst_data.retrieval.gpu_contract import verify_source_bundle  # noqa: E402

_SECRET_PATTERNS = (
    re.compile(r"(?i)(sk-[A-Za-z0-9_-]{8,})"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password|credential|authorization)[=: ]+[^\s,;}\]]+"),
)


def _git_head(repo: Path = REPO_ROOT) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("git rev-parse HEAD failed")
    return result.stdout.strip()


def _git_status_porcelain(repo: Path = REPO_ROOT) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("git status --porcelain failed")
    return result.stdout


def _require_head(expected_head: str, repo: Path = REPO_ROOT) -> None:
    if not isinstance(expected_head, str) or _HEX40.fullmatch(expected_head) is None:
        raise ValueError("--expected-implementation-head must be 40 lowercase hex")
    if _git_head(repo) != expected_head:
        raise ValueError("--expected-implementation-head does not match git HEAD")


def _frozen_live_db_paths(repo_root: Path = REPO_ROOT) -> list[Path]:
    """Authoritative frozen/live DB paths; always includes the frozen snapshot."""
    paths: list[Path] = [FROZEN_SNAPSHOT_PATH]
    snapshot_manifest = repo_root / "data" / "manifests" / "active_data_snapshot.json"
    if snapshot_manifest.is_file():
        try:
            payload = json.loads(snapshot_manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        candidate = payload.get("path") or payload.get("db_path")
        if isinstance(candidate, str) and candidate:
            paths.append(Path(candidate))
    working_db = repo_root / "data" / "manifests" / "b2o_working_db_path.txt"
    if working_db.is_file():
        text = working_db.read_text(encoding="utf-8").strip()
        if text:
            paths.append(Path(text))
    app_default = repo_root / ".local" / "live_runtime.db"
    if app_default.exists():
        paths.append(app_default)
    return paths


def _reject_aliased_db(path: Path, repo_root: Path = REPO_ROOT) -> None:
    """Reject symlink/hardlink/samefile aliases to frozen or live DBs."""
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValueError("DB path must not be a symlink")
    resolved = candidate.resolve()
    for forbidden in _frozen_live_db_paths(repo_root):
        if not forbidden.exists():
            continue
        if resolved == forbidden.resolve():
            raise ValueError("DB path resolves to a forbidden frozen/live DB")
        try:
            if os.path.samefile(resolved, forbidden):
                raise ValueError("DB path aliases a forbidden frozen/live DB")
        except FileNotFoundError:
            pass



def _validate_hex64(value: str, *, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        ch not in "0123456789abcdef" for ch in value
    ):
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return parsed


def _resolve_required(path: Path, *, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    return candidate.resolve()


def _open_derivative(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _seal_git_revision(benchmark_path: Path, q005_path: Path) -> str:
    from catalyst_data.sec.m3_8b_validator import validate_m3_8b_operator_inputs

    benchmark = json.loads(Path(benchmark_path).read_text(encoding="utf-8"))
    q005 = json.loads(Path(q005_path).read_text(encoding="utf-8"))
    benchmark_revision = benchmark.get("git_revision")
    q005_revision = q005.get("git_revision")
    if (
        not isinstance(benchmark_revision, str)
        or benchmark_revision != q005_revision
        or _HEX40.fullmatch(benchmark_revision) is None
    ):
        raise ValueError("seal records must carry the same 40-hex git_revision")
    validate_m3_8b_operator_inputs(
        benchmark_path,
        q005_path,
        expected_git_revision=benchmark_revision,
    )
    return benchmark_revision


def _decode_payload(content_raw: bytes) -> Any:
    try:
        decoded = zlib.decompress(content_raw)
    except zlib.error:
        decoded = content_raw
    try:
        return json.loads(decoded.decode("utf-8"))
    except Exception:
        return None


def recover_accepted_time_candidate(payload: object, *, accession: str):
    """Recover accepted time from payload or the official payload['filings'].

    Archived EDGAR submissions JSON nests ``recent`` under
    ``payload["filings"]["recent"]``; the locked ``recover_accepted_time`` reads
    ``raw_payload["recent"]``, so the candidate is the object that contains
    ``recent``. Returns an aware datetime or None (never invented).
    """
    from catalyst_data.sec.accepted_time import recover_accepted_time

    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("recent"), dict):
        recovered = recover_accepted_time(payload, accession=accession)
        if recovered is not None:
            return recovered
    if isinstance(payload.get("filings"), dict):
        return recover_accepted_time(payload["filings"], accession=accession)
    return None


def _step_derivative_migration(conn: sqlite3.Connection, derivative: Path) -> dict[str, Any]:
    from catalyst_data.migrations import CURRENT_SCHEMA_VERSION, run_migrations

    run_migrations(conn)
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version != CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f"derivative user_version {version} != expected {CURRENT_SCHEMA_VERSION}"
        )
    return {"user_version": version}


def _step_accepted_time(conn: sqlite3.Connection, q005_path: Path) -> dict[str, Any]:
    from catalyst_data.sec.eligible_at import (
        derive_eligible_at,
        persist_filing_temporal_repair,
    )

    q005 = json.loads(Path(q005_path).read_text(encoding="utf-8"))
    approve = q005.get("decision") == "approve_latest_plausible_instant"
    recovered_by_accession: dict[str, Any] = {}
    rows = conn.execute(
        "SELECT content_raw FROM raw_assets WHERE source_type='sec_filings'"
    ).fetchall()
    for row in rows:
        payload = _decode_payload(row["content_raw"])
        if not isinstance(payload, dict):
            continue
        candidates: list[Any] = []
        if isinstance(payload.get("recent"), dict):
            candidates.append(payload)
        if isinstance(payload.get("filings"), dict):
            candidates.append(payload["filings"])
        for candidate in candidates:
            recent = candidate.get("recent")
            accessions = (
                recent.get("accessionNumber") if isinstance(recent, dict) else None
            )
            if not isinstance(accessions, list):
                continue
            for accession in accessions:
                if accession in recovered_by_accession:
                    continue
                accepted = recover_accepted_time_candidate(
                    candidate, accession=accession
                )
                if accepted is not None:
                    recovered_by_accession[accession] = accepted

    persisted = 0
    recovered_count = 0
    for filing in conn.execute("SELECT * FROM filings").fetchall():
        accepted = recovered_by_accession.get(filing["accession_number"])
        if accepted is not None:
            recovered_count += 1
        result = derive_eligible_at(
            filing, accepted, approve_latest_plausible=approve
        )
        persist_filing_temporal_repair(
            conn,
            filing_id=filing["filing_id"],
            result=result,
            accepted_time=accepted,
        )
        persisted += 1
    null_reason = int(
        conn.execute(
            "SELECT COUNT(*) FROM filings WHERE eligible_at_reason IS NULL"
        ).fetchone()[0]
    )
    if null_reason:
        raise ValueError(
            f"{null_reason} filings missing eligible_at_reason after accepted-time persistence"
        )
    return {
        "accepted_time_recovered": recovered_count,
        "persisted": persisted,
    }


def _retained_primary_bytes(
    conn: sqlite3.Connection, row: Any
) -> bytes | None:
    """Return retained primary-document bytes for a filing when present."""
    if not row["raw_asset_id"]:
        return None
    raw = conn.execute(
        "SELECT content_raw FROM raw_assets WHERE asset_id=?",
        (row["raw_asset_id"],),
    ).fetchone()
    if raw is None or not raw["content_raw"]:
        return None
    payload = _decode_payload(raw["content_raw"])
    if not isinstance(payload, dict):
        return None
    text = payload.get("document_text")
    if isinstance(text, str) and text.strip():
        return text.encode("utf-8")
    return None


def _step_sec_reparse(conn: sqlite3.Connection) -> dict[str, Any]:
    from catalyst_data.sec.extract import SEC_EXTRACT_PARSER_VERSION
    from catalyst_data.sec.reparse import (
        FilingParseResult,
        reparse_filing,
        persist_filing_document_reparse,
    )

    reparsed = 0
    skipped = 0
    rows = conn.execute(
        """SELECT fd.filing_id, fd.document_id, fd.text AS stored_text,
                  f.accession_number, f.raw_asset_id
           FROM filing_documents fd
           JOIN filings f ON f.filing_id = fd.filing_id"""
    ).fetchall()
    for row in rows:
        # Retained primary payload takes precedence; otherwise reparse the
        # stored extracted text (UTF-8). Every filing_documents row receives a
        # persisted M3-4 repair so require_repairs=True backfill passes;
        # unbindable empty documents get a not_applicable repair (Batch B skip,
        # never fabricated into HTML).
        primary_bytes = _retained_primary_bytes(conn, row)
        if primary_bytes is None and row["stored_text"]:
            primary_bytes = str(row["stored_text"]).encode("utf-8")
        if primary_bytes is None:
            persist_filing_document_reparse(
                conn,
                filing_id=row["filing_id"],
                document_id=row["document_id"],
                result=FilingParseResult(
                    accession=row["accession_number"],
                    parser_version=SEC_EXTRACT_PARSER_VERSION,
                    primary_document_extracted=False,
                    document_hash=None,
                    parse_quality="not_applicable",
                    sections=(),
                ),
                extracted_text=None,
            )
            skipped += 1
            continue
        result = reparse_filing(
            primary_bytes,
            row["accession_number"],
            parser_version=SEC_EXTRACT_PARSER_VERSION,
        )
        persist_filing_document_reparse(
            conn,
            filing_id=row["filing_id"],
            document_id=row["document_id"],
            result=result,
            extracted_text=(
                primary_bytes.decode("utf-8", "replace")
                if result.primary_document_extracted
                else None
            ),
        )
        if result.primary_document_extracted:
            reparsed += 1
        else:
            skipped += 1
    return {"reparsed": reparsed, "skipped": skipped}


def _step_news_persistence(conn: sqlite3.Connection) -> dict[str, Any]:
    from catalyst_data.articles.body_recovery import (
        persist_article_content_repair,
        recover_body,
    )
    from catalyst_data.articles.url_normalize import normalize_url

    repaired = 0
    skipped = 0
    rows = conn.execute(
        """SELECT a.*, r.content_raw AS content_raw
           FROM articles a JOIN raw_assets r ON r.asset_id = a.raw_asset_id"""
    ).fetchall()
    for row in rows:
        # sqlite3.Row is not a Mapping with .get; pass a dict to recover_body.
        row_dict = dict(row)
        payload = _decode_payload(row["content_raw"])
        if payload is None:
            skipped += 1
            continue
        result = recover_body(row_dict, payload)
        persist_article_content_repair(
            conn,
            article_id=row_dict["article_id"],
            normalized_url=normalize_url(row_dict.get("article_url")),
            result=result,
        )
        repaired += 1
    return {"repaired": repaired, "skipped": skipped}


def _step_m35b_backfill(conn: sqlite3.Connection) -> dict[str, Any]:
    from catalyst_data.canonical.backfill import backfill_from_subtypes

    result = backfill_from_subtypes(conn, require_repairs=True)
    return {
        "assets": result.assets,
        "content_versions": result.content_versions,
        "associations": result.associations,
    }


def _step_m36_dedup(conn: sqlite3.Connection) -> dict[str, Any]:
    from catalyst_data.canonical.dedup_independence import (
        compute_dedup_clusters,
        compute_independence_groups,
        load_canonical_assets,
        persist_dedup_independence,
    )

    assets = load_canonical_assets(conn)
    dedup = compute_dedup_clusters(assets)
    independence = compute_independence_groups(assets)
    persist_dedup_independence(conn, dedup, independence)
    return {"dedup_clusters": len(dedup.cluster_id_by_asset)}


def _step_audit(conn: sqlite3.Connection) -> dict[str, Any]:
    from catalyst_data.canonical.audit import audit_canonical

    result = audit_canonical(conn)
    if result.failures:
        raise ValueError("canonical audit failed: " + ",".join(result.failures[:5]))
    return {"failures": len(result.failures)}


def _step_data01(
    conn: sqlite3.Connection,
    benchmark_path: Path,
    q005_path: Path,
    git_revision: str,
) -> dict[str, Any]:
    from catalyst_data.sec.gate_report import (
        benchmark_sec_parse_report_from_operator_inputs,
    )

    report = benchmark_sec_parse_report_from_operator_inputs(
        conn,
        benchmark_path,
        q005_path,
        expected_git_revision=git_revision,
    )
    return {
        "denominator": report.denominator,
        "numerator": report.numerator,
        "gate_passed": report.gate_passed,
        "accepted_time_recovered_count": report.accepted_time_recovered_count,
        "fail_closed_eligibility_count": report.fail_closed_eligibility_count,
    }


def _step_corpus_candidate(
    conn: sqlite3.Connection,
    *,
    certified_snapshot_identity: str,
    source_bundle_output_root: Path,
    snapshot_id: str,
    probe_report_id: str,
    postbuild_readiness_id: str,
) -> dict[str, Any]:
    from catalyst_data.corpus.streaming_publication import (
        build_candidate_fts,
        stage_corpus_candidate,
    )

    candidate = stage_corpus_candidate(
        conn,
        certified_snapshot_identity=certified_snapshot_identity,
        profile_versions={"news": "news_v2", "filing": "filing_v3"},
        source_bundle_output_root=source_bundle_output_root,
        snapshot_id=snapshot_id,
        probe_report_id=probe_report_id,
        postbuild_readiness_id=postbuild_readiness_id,
    )
    lexical = build_candidate_fts(conn, build_id=candidate.build_id)
    return {
        "build_id": candidate.build_id,
        "corpus_manifest_id": candidate.manifest_id,
        "chunk_count": candidate.chunk_count,
        "source_bundle_id": candidate.source_bundle_id,
        "source_bundle_path": str(candidate.source_bundle_path),
        "lexical_digest": lexical.digest,
        "lexical_row_count": lexical.row_count,
    }


def _step_bundle_export(
    conn: sqlite3.Connection, candidate: dict[str, Any]
) -> dict[str, Any]:
    from catalyst_data.retrieval.gpu_contract import verify_source_bundle

    verified = verify_source_bundle(
        Path(candidate["source_bundle_path"]),
        expected_source_bundle_id=candidate["source_bundle_id"],
    )
    return {
        "source_bundle_id": verified.source_bundle_id,
        "chunk_count": verified.chunk_count,
        "bundle_path": str(verified.path),
    }


def _redact_error(text: str) -> str:
    redacted = str(text)
    ua = os.environ.get("SEC_USER_AGENT")
    if ua:
        redacted = redacted.replace(ua, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    # Replace absolute path-like sequences before home replacement so trailing
    # path components (e.g. id_rsa) never leak.
    redacted = re.sub(r"(?<![\w])(?:/[A-Za-z0-9._~-]+){2,}", "[PATH]", redacted)
    home = str(Path.home())
    if home and home != "/":
        redacted = redacted.replace(home, "[HOME]")
    return redacted


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_resume(args: argparse.Namespace) -> dict[str, Any]:
    """Resume reconciliation + candidate bundle + FTS for an existing build.

    L4 order: L1 bind/validate -> additive schema ensure -> read-only audit ->
    real DATA-01 -> gate check -> resume_candidate_reconciliation ->
    export_candidate_source_bundle (M3-9) -> build_candidate_fts (M3-10) ->
    verify_source_bundle -> preparation evidence. Forbidden seams are never
    called: derivative migration, accepted time, sec reparse, news persistence,
    backfill, dedup, stage_corpus_candidate, recover_primary, canonical records,
    _manifest_phase, _cutover, retrieval FTS5 builder, _build_id.
    """
    from catalyst_data.corpus.streaming_publication import (
        build_candidate_fts,
        ensure_streaming_publication_schema,
        explain_reconciliation_dml,
        reconciliation_resume_state,
        resume_candidate_reconciliation,
    )
    from catalyst_data.retrieval.gpu_contract import verify_source_bundle
    from catalyst_data.retrieval.source_bundle import export_candidate_source_bundle

    derivative = _resolve_required(args.derivative, label="--derivative")
    _reject_aliased_db(derivative)
    benchmark = _resolve_required(args.benchmark_manifest, label="--benchmark-manifest")
    q005 = _resolve_required(args.q005_approval, label="--q005-approval")
    bundle_root = _resolve_required(
        args.source_bundle_output_root, label="--source-bundle-output-root"
    )
    evidence_path = _resolve_required(
        args.preparation_evidence, label="--preparation-evidence"
    )
    snapshot_id = _validate_hex64(args.snapshot_id, label="--snapshot-id")
    probe_report_id = _validate_hex64(
        args.probe_report_id, label="--probe-report-id"
    )
    postbuild_readiness_id = _validate_hex64(
        args.postbuild_readiness_id, label="--postbuild-readiness-id"
    )
    build_id = _validate_hex64(args.resume_build_id, label="--resume-build-id")
    git_revision = _seal_git_revision(benchmark, q005)

    flags = {"deadline": False, "operator_interrupt": False}
    previous_handlers: dict[int, Any] = {}

    def operator_interrupt_handler(signum: int, frame: Any) -> None:
        flags["operator_interrupt"] = True

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, operator_interrupt_handler)

    conn = _open_derivative(derivative)
    try:
        original_status, _ = reconciliation_resume_state(
            conn, build_id=build_id
        )
        row = conn.execute(
            "SELECT updated_at FROM corpus_publication_builds WHERE build_id=?",
            (build_id,),
        ).fetchone()
        if row is None:
            raise ValueError("resume build row not found")
        original_manifest_ready_at = str(row[0])

        if args.dry_run:
            ensure_streaming_publication_schema(conn)
            reconciliation_resume_state(conn, build_id=build_id)
            explain_reconciliation_dml(conn, build_id=build_id)
            return {
                "schema_version": "preparation_evidence_v1",
                "git_revision": git_revision,
                "dry_run": True,
                "derivative": str(derivative),
                "snapshot_id": snapshot_id,
                "probe_report_id": probe_report_id,
                "postbuild_readiness_id": postbuild_readiness_id,
                "resume": {
                    "implementation_head": args.expected_implementation_head,
                    "build_id": build_id,
                    "original_status": original_status,
                    "original_manifest_ready_at": original_manifest_ready_at,
                    "original_git_revision": git_revision,
                },
            }

        ensure_streaming_publication_schema(conn)
        reconciliation_resume_state(conn, build_id=build_id)
        evidence: dict[str, Any] = {
            "schema_version": "preparation_evidence_v1",
            "git_revision": git_revision,
            "generated_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "snapshot_id": snapshot_id,
            "probe_report_id": probe_report_id,
            "postbuild_readiness_id": postbuild_readiness_id,
        }
        evidence["audit"] = _step_audit(conn)
        evidence["data01"] = _step_data01(conn, benchmark, q005, git_revision)
        if not evidence["data01"].get("gate_passed"):
            raise ValueError(
                "DATA-01 gate failed; refusing to resume a candidate"
            )
        summary = resume_candidate_reconciliation(
            conn,
            build_id=build_id,
            deadline=args.reconciliation_deadline_seconds,
            operator_interrupt=flags,
        )
        manifest_row = conn.execute(
            "SELECT manifest_id FROM corpus_publication_builds WHERE build_id=?",
            (build_id,),
        ).fetchone()
        if manifest_row is None or not manifest_row[0]:
            raise ValueError("resumed build has no committed manifest_id")
        manifest_id = str(manifest_row[0])
        bundle_id, bundle_path = export_candidate_source_bundle(
            conn,
            build_id=build_id,
            manifest_id=manifest_id,
            snapshot_id=snapshot_id,
            probe_report_id=probe_report_id,
            postbuild_readiness_id=postbuild_readiness_id,
            output_root=bundle_root,
        )
        lexical = build_candidate_fts(conn, build_id=build_id)
        verified = verify_source_bundle(
            Path(bundle_path),
            expected_source_bundle_id=bundle_id,
        )
        evidence["resume"] = {
            "implementation_head": args.expected_implementation_head,
            "build_id": build_id,
            "corpus_manifest_id": manifest_id,
            "original_status": original_status,
            "original_manifest_ready_at": original_manifest_ready_at,
            "original_git_revision": git_revision,
        }
        evidence["reconciliation"] = {
            "to_embed_count": summary.to_embed_count,
            "metadata_update_count": summary.metadata_update_count,
            "tombstone_count": summary.tombstone_count,
        }
        evidence["build_id"] = build_id
        evidence["corpus_manifest_id"] = manifest_id
        evidence["chunk_count"] = lexical.row_count
        evidence["lexical_digest"] = lexical.digest
        evidence["source_bundle_id"] = verified.source_bundle_id
        evidence["source_bundle_path"] = str(verified.path)
    finally:
        conn.close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    _write_json_atomic(evidence_path, evidence)
    return evidence


def _prepare(args: argparse.Namespace) -> dict[str, Any]:
    _require_head(args.expected_implementation_head)
    derivative = _resolve_required(args.derivative, label="--derivative")
    _reject_aliased_db(derivative)
    benchmark = _resolve_required(args.benchmark_manifest, label="--benchmark-manifest")
    q005 = _resolve_required(args.q005_approval, label="--q005-approval")
    bundle_root = _resolve_required(
        args.source_bundle_output_root, label="--source-bundle-output-root"
    )
    evidence_path = _resolve_required(
        args.preparation_evidence, label="--preparation-evidence"
    )
    snapshot_id = _validate_hex64(args.snapshot_id, label="--snapshot-id")
    probe_report_id = _validate_hex64(
        args.probe_report_id, label="--probe-report-id"
    )
    postbuild_readiness_id = _validate_hex64(
        args.postbuild_readiness_id, label="--postbuild-readiness-id"
    )
    git_revision = _seal_git_revision(benchmark, q005)

    if args.resume_build_id is not None:
        return _prepare_resume(args)

    if args.dry_run:
        return {
            "schema_version": "preparation_evidence_v1",
            "git_revision": git_revision,
            "dry_run": True,
            "derivative": str(derivative),
            "snapshot_id": snapshot_id,
            "probe_report_id": probe_report_id,
            "postbuild_readiness_id": postbuild_readiness_id,
        }

    conn = _open_derivative(derivative)
    try:
        evidence: dict[str, Any] = {
            "schema_version": "preparation_evidence_v1",
            "git_revision": git_revision,
            "generated_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "snapshot_id": snapshot_id,
            "probe_report_id": probe_report_id,
            "postbuild_readiness_id": postbuild_readiness_id,
        }
        evidence["derivative"] = _step_derivative_migration(conn, derivative)
        evidence["accepted_time"] = _step_accepted_time(conn, q005)
        evidence["sec_reparse"] = _step_sec_reparse(conn)
        evidence["news_persistence"] = _step_news_persistence(conn)
        evidence["m35b_backfill"] = _step_m35b_backfill(conn)
        evidence["dedup_independence"] = _step_m36_dedup(conn)
        evidence["audit"] = _step_audit(conn)
        evidence["data01"] = _step_data01(conn, benchmark, q005, git_revision)
        if not evidence["data01"].get("gate_passed"):
            raise ValueError(
                "DATA-01 gate failed; refusing to build/export a candidate"
            )
        candidate = _step_corpus_candidate(
            conn,
            certified_snapshot_identity=snapshot_id,
            source_bundle_output_root=bundle_root,
            snapshot_id=snapshot_id,
            probe_report_id=probe_report_id,
            postbuild_readiness_id=postbuild_readiness_id,
        )
        evidence["candidate"] = candidate
        evidence["bundle"] = _step_bundle_export(conn, candidate)
        evidence["probe_report_id"] = probe_report_id
        evidence["postbuild_readiness_id"] = postbuild_readiness_id
        evidence["build_id"] = candidate["build_id"]
        evidence["corpus_manifest_id"] = candidate["corpus_manifest_id"]
        evidence["chunk_count"] = candidate["chunk_count"]
        evidence["lexical_digest"] = candidate["lexical_digest"]
        evidence["source_bundle_id"] = candidate["source_bundle_id"]
    finally:
        conn.close()
    _write_json_atomic(evidence_path, evidence)
    return evidence


def _read_artifact_manifest(artifact_dir: Path) -> Any:
    from catalyst_data.retrieval.index_manifest import IndexManifest

    manifest_path = Path(artifact_dir) / "index_manifest.json"
    if not manifest_path.is_file():
        raise ValueError("embedding artifact index_manifest.json missing")
    return IndexManifest.from_dict(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )


def _promote(args: argparse.Namespace) -> dict[str, Any]:
    _require_head(args.expected_implementation_head)
    derivative = _resolve_required(args.derivative, label="--derivative")
    _reject_aliased_db(derivative)
    benchmark = _resolve_required(args.benchmark_manifest, label="--benchmark-manifest")
    q005 = _resolve_required(args.q005_approval, label="--q005-approval")
    bundle = _resolve_required(args.source_bundle, label="--source-bundle")
    artifact_dir = _resolve_required(
        args.embedding_artifact_dir, label="--embedding-artifact-dir"
    )
    candidate_dir = _resolve_required(
        args.candidate_manifest_dir, label="--candidate-manifest-dir"
    )
    active_path = _resolve_required(args.active_generation, label="--active-generation")
    journal_path = _resolve_required(args.journal, label="--journal")
    evidence_path = _resolve_required(
        args.promotion_evidence, label="--promotion-evidence"
    )
    git_revision = _seal_git_revision(benchmark, q005)

    # The exported source bundle is the authenticated preparation carrier:
    # it binds build identities (corpus_manifest_id, chunk_count, probe/
    # postbuild readiness) that must agree with the GPU index manifest.
    manifest = _read_artifact_manifest(artifact_dir)
    verified = verify_source_bundle(
        bundle,
        expected_source_bundle_id=manifest.source_bundle_id,
        expected_snapshot_id=manifest.snapshot_id,
        expected_corpus_manifest_id=manifest.corpus_manifest_id,
        expected_probe_report_id=manifest.probe_report_id,
        expected_postbuild_readiness_id=manifest.postbuild_readiness_id,
    )
    if manifest.vector_count != verified.chunk_count:
        raise ValueError("embedding artifact vector_count mismatch")

    if args.dry_run:
        return {
            "schema_version": "promotion_evidence_v1",
            "git_revision": git_revision,
            "dry_run": True,
            "build_id": "",
            "corpus_manifest_id": verified.corpus_manifest_id,
            "index_manifest_id": manifest.index_manifest_id,
        }

    conn = _open_derivative(derivative)
    try:
        build_row = conn.execute(
            """SELECT build_id, lexical_digest, chunk_count, lexical_row_count
               FROM corpus_publication_builds
               WHERE manifest_id=? AND lexical_ready=1""",
            (verified.corpus_manifest_id,),
        ).fetchone()
        if build_row is None:
            raise ValueError(
                "prepared candidate build missing on derivative for source bundle"
            )
        build_id = str(build_row["build_id"])
        lexical_digest = str(build_row["lexical_digest"])
        if int(build_row["chunk_count"] or 0) != verified.chunk_count:
            raise ValueError("candidate build chunk_count mismatch")
        if int(build_row["lexical_row_count"] or 0) != verified.chunk_count:
            raise ValueError("candidate lexical row_count mismatch")
        if not lexical_digest:
            raise ValueError("candidate lexical_digest missing")

        dense_candidate = stage_dense(
            candidate_dir,
            embedding_artifact_dir=artifact_dir,
            new_index_manifest=manifest,
            expected_chunk_count=verified.chunk_count,
            source_conn=conn,
            source_build_id=build_id,
        )
        if dense_candidate.corpus_manifest_id != verified.corpus_manifest_id:
            raise ValueError("dense candidate corpus_manifest_id mismatch")
        if dense_candidate.chunk_count != verified.chunk_count:
            raise ValueError("dense candidate chunk_count mismatch")
        if dense_candidate.source_bundle_id != verified.source_bundle_id:
            raise ValueError("dense candidate source_bundle_id mismatch")

        result = promote_v1_generation(
            conn,
            journal_path=journal_path,
            build_id=build_id,
            corpus_manifest_id=verified.corpus_manifest_id,
            lexical_digest=lexical_digest,
            dense_candidate=dense_candidate,
            active_generation_path=active_path,
        )
        if result.state != "COMMITTED" or not result.admitted:
            raise ValueError(
                "promotion did not commit; refusing to write promotion evidence"
            )
    finally:
        conn.close()

    promotion_evidence = {
        "schema_version": "promotion_evidence_v1",
        "build_id": build_id,
        "corpus_manifest_id": verified.corpus_manifest_id,
        "dense_index_manifest_id": result.dense_index_manifest_id,
        "index_manifest_id": manifest.index_manifest_id,
        "source_bundle_id": verified.source_bundle_id,
        "git_revision": git_revision,
        "state": result.state,
        "promotion_id": result.promotion_id,
        "journal_path": str(journal_path),
        "gpu": {
            "model": manifest.model_name,
            "revision": manifest.model_revision,
            "dimension": manifest.dimension,
            "vector_count": manifest.vector_count,
        },
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _write_json_atomic(evidence_path, promotion_evidence)
    return promotion_evidence


def _rollback(args: argparse.Namespace) -> dict[str, Any]:
    _require_head(args.expected_implementation_head)
    derivative = _resolve_required(args.derivative, label="--derivative")
    _reject_aliased_db(derivative)
    active_path = _resolve_required(args.active_generation, label="--active-generation")
    journal_path = _resolve_required(args.journal, label="--journal")
    conn = _open_derivative(derivative)
    try:
        result = rollback_v1_generation(
            conn,
            journal_path=journal_path,
            active_generation_path=active_path,
        )
    finally:
        conn.close()
    return {
        "schema_version": "promotion_evidence_v1",
        "state": result.state,
        "promotion_id": result.promotion_id,
        "build_id": result.build_id,
        "corpus_manifest_id": result.corpus_manifest_id,
        "dense_index_manifest_id": result.dense_index_manifest_id,
        "admitted": result.admitted,
    }


def _recover_sec_primary(args: argparse.Namespace) -> dict[str, Any]:
    """Recover missing SEC primary documents on a fresh derivative.

    Live EDGAR recovery stage (supervisor 2026-08-24). Requires a separately
    verified derivative; the frozen snapshot is opened read-only (SHA-verified
    before and after). The offline prepare step later runs on the same
    derivative and never performs network work.
    """
    _require_head(args.expected_implementation_head)
    derivative = _resolve_required(args.derivative, label="--derivative")
    _reject_aliased_db(derivative)
    benchmark = _resolve_required(args.benchmark_manifest, label="--benchmark-manifest")
    q005 = _resolve_required(args.q005_approval, label="--q005-approval")
    frozen = _resolve_required(args.frozen_snapshot, label="--frozen-snapshot")
    checkpoint = _resolve_required(args.checkpoint, label="--checkpoint")
    report_path = _resolve_required(args.recovery_report, label="--recovery-report")
    rate = args.rate_per_second
    if not (0.1 <= rate <= 5.0):
        raise ValueError("--rate-per-second must be within [0.1, 5.0]")
    if args.limit is not None and not args.dry_run:
        raise ValueError(
            "--limit is test-only and may not author a production recovery report"
        )
    if not Path(frozen).is_file():
        raise ValueError("--frozen-snapshot must be an existing file")

    git_revision = _seal_git_revision(benchmark, q005)
    benchmark_json = json.loads(Path(benchmark).read_text(encoding="utf-8"))
    accession_list_sha256 = str(benchmark_json["case_list_sha256"])
    accessions = list(benchmark_json["ordered_unique_accession_ids"])
    frozen_before = _sha256_file(frozen)

    from catalyst_data.sec.recover_primary import compute_missing_work

    if args.dry_run:
        conn = _open_derivative(derivative)
        try:
            missing, already_present = compute_missing_work(conn, accessions)
        finally:
            conn.close()
        return {
            "schema_version": "sec_primary_recovery_report_v1",
            "git_revision": git_revision,
            "dry_run": True,
            "work_set_total": len(accessions),
            "requested": len(missing),
            "already_present": already_present,
            "accession_list_sha256": accession_list_sha256,
            "frozen_sha256_before": frozen_before,
            "derivative": str(derivative),
        }

    user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent or not user_agent.strip():
        raise ValueError(
            "SEC_USER_AGENT environment variable is required and must not be empty"
        )

    import asyncio
    import signal

    from catalyst_data.config import RatePolicy
    from catalyst_data.manifests.universe import sha256_identity
    from catalyst_data.rate_limiter import TokenBucketLimiter
    from catalyst_data.sec import recover_primary as _recover_primary_mod

    conn = _open_derivative(derivative)
    try:
        _step_derivative_migration(conn, derivative)
        limiter = TokenBucketLimiter(
            RatePolicy(
                min_interval_sec=1.0 / rate,
                max_concurrent=2,
                daily_budget=None,
            )
        )
        run_id = sha256_identity(
            {
                "purpose": "recover-sec-primary",
                "derivative": str(derivative),
                "git_revision": git_revision,
            }
        )

        async def live_fetch(url: str):
            return await _recover_primary_mod.http_fetch_document(
                url, user_agent=user_agent
            )

        # Graceful SIGINT/SIGTERM: set a stop flag checked after each persist so
        # the checkpoint is current and the run exits resumable, never mid-write.
        interrupted = [False]
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: interrupted.__setitem__(0, True))
            except (NotImplementedError, RuntimeError):
                pass
        try:
            counters = loop.run_until_complete(
                _recover_primary_mod.recover_primary_documents(
                    conn,
                    accessions=accessions,
                    fetch_document=live_fetch,
                    run_id=run_id,
                    checkpoint_path=checkpoint,
                    limiter=limiter,
                    limit=args.limit,
                    stop_check=lambda: interrupted[0],
                )
            )
        finally:
            loop.close()
        filings_count = int(
            conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0]
        )
    finally:
        conn.close()

    if filings_count != len(accessions):
        raise ValueError(
            f"filings count {filings_count} != sealed work set {len(accessions)}"
        )
    frozen_after = _sha256_file(frozen)
    if frozen_before != frozen_after:
        raise ValueError(
            "frozen snapshot changed during recovery; refusing recovery report"
        )
    derivative_sha = _sha256_file(derivative)

    report: dict[str, Any] = {
        "schema_version": "sec_primary_recovery_report_v1",
        "git_revision": git_revision,
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "accession_list_sha256": accession_list_sha256,
        "frozen_sha256_before": frozen_before,
        "frozen_sha256_after": frozen_after,
        "derivative_sha256": derivative_sha,
        "work_set_total": len(accessions),
        "filings_count": filings_count,
        "requested": counters["requested"],
        "already_present": counters["already_present"],
        "succeeded": counters["succeeded"],
        "pdf_skipped": counters["pdf_skipped"],
        "empty": counters["empty"],
        "permanent_404": counters["permanent_404"],
        "retry_exhausted": counters["retry_exhausted"],
        "transient_failed": counters["transient_failed"],
        "http_403": counters["http_403"],
        "rejected_content": counters["rejected_content"],
        "bytes_written": counters["bytes_written"],
        "remaining": counters["remaining"],
        "requests_made": counters["requests_made"],
        "interrupted": counters["interrupted"],
        "derivative": str(derivative),
        "checkpoint": str(checkpoint),
    }
    _write_json_atomic(report_path, report)
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    prepare = subparsers.add_parser("prepare", help="prepare a generation on the derivative")
    prepare.add_argument("--derivative", required=True, type=Path)
    prepare.add_argument("--benchmark-manifest", required=True, type=Path)
    prepare.add_argument("--q005-approval", required=True, type=Path)
    prepare.add_argument("--source-bundle-output-root", required=True, type=Path)
    prepare.add_argument("--preparation-evidence", required=True, type=Path)
    prepare.add_argument("--snapshot-id", required=True)
    prepare.add_argument("--probe-report-id", required=True)
    prepare.add_argument("--postbuild-readiness-id", required=True)
    prepare.add_argument("--expected-implementation-head", required=True)
    prepare.add_argument("--resume-build-id", default=None,
                        help="resume reconciliation/FTS for an existing build id")
    prepare.add_argument("--reconciliation-deadline-seconds", type=_positive_float,
                         default=900.0,
                         help="per-phase reconciliation deadline in seconds (default 900.0)")
    prepare.add_argument("--dry-run", action="store_true")

    promote = subparsers.add_parser("promote", help="promote a prepared generation")
    promote.add_argument("--derivative", required=True, type=Path)
    promote.add_argument("--benchmark-manifest", required=True, type=Path)
    promote.add_argument("--q005-approval", required=True, type=Path)
    promote.add_argument("--source-bundle", required=True, type=Path)
    promote.add_argument("--embedding-artifact-dir", required=True, type=Path)
    promote.add_argument("--candidate-manifest-dir", required=True, type=Path)
    promote.add_argument("--active-generation", required=True, type=Path)
    promote.add_argument("--journal", required=True, type=Path)
    promote.add_argument("--promotion-evidence", required=True, type=Path)
    promote.add_argument("--expected-implementation-head", required=True)
    promote.add_argument("--dry-run", action="store_true")

    rollback = subparsers.add_parser("rollback", help="roll back to the prior generation")
    rollback.add_argument("--derivative", required=True, type=Path)
    rollback.add_argument("--active-generation", required=True, type=Path)
    rollback.add_argument("--journal", required=True, type=Path)
    rollback.add_argument("--expected-implementation-head", required=True)

    recover = subparsers.add_parser(
        "recover-sec-primary",
        help="recover missing SEC primary documents on a fresh derivative (live EDGAR; prepare stays offline)",
    )
    recover.add_argument("--derivative", required=True, type=Path)
    recover.add_argument("--benchmark-manifest", required=True, type=Path)
    recover.add_argument("--q005-approval", required=True, type=Path)
    recover.add_argument("--frozen-snapshot", required=True, type=Path)
    recover.add_argument("--checkpoint", required=True, type=Path)
    recover.add_argument("--recovery-report", required=True, type=Path)
    recover.add_argument("--expected-implementation-head", required=True)
    recover.add_argument(
        "--rate-per-second", type=float, default=4.0,
        help="global SEC request rate cap for this job (default 4.0, max 5.0)",
    )
    recover.add_argument("--dry-run", action="store_true")
    # Test-only bound; suppressed from --help and fails closed when set on a
    # production (non-dry-run) report path.
    recover.add_argument("--limit", type=int, default=None, help=argparse.SUPPRESS)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.subcommand == "prepare":
            result = _prepare(args)
        elif args.subcommand == "promote":
            result = _promote(args)
        elif args.subcommand == "rollback":
            result = _rollback(args)
        elif args.subcommand == "recover-sec-primary":
            result = _recover_sec_primary(args)
        else:  # pragma: no cover - argparse requires a subcommand
            raise ValueError("unknown subcommand")
        print(json.dumps({"ok": True, "subcommand": args.subcommand, **result}, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "subcommand": args.subcommand, "error": _redact_error(str(exc))},
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
