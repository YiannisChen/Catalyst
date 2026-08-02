"""Production-owned Pre-B6 corpus and lexical promotion gates."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from catalyst_data.corpus.streaming_publication import served_chunks_relation
from catalyst_data.manifests.universe import RATIFIED_TICKERS, sha256_identity


PROBE_POLICY_REVISION = "pre_b6_40x40_v1"
PROBE_REPORT_SCHEMA_VERSION = "pre_b6_probe_report_v1"
SEARCHABLE_STATUSES = ("active", "pending_embedding", "embedded", "metadata_only")
SMOKE_STOPWORDS_V1 = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "with",
    }
)
_WORD_RE = re.compile(r"[a-z0-9]+")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class PreB6ProbeGateError(ValueError):
    """Raised after a deterministic failing probe report has been persisted."""

    def __init__(self, message: str, *, report: Mapping[str, Any]):
        super().__init__(message)
        self.report = dict(report)


class LexicalSmokeError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        reason: str,
        mode_served: str | None = None,
        query_terms: Sequence[str] = (),
        anchor_chunk_id: str | None = None,
        anchor_document_id: str | None = None,
        cutoff: str | None = None,
    ):
        super().__init__(message)
        self.reason = reason
        self.mode_served = mode_served
        self.query_terms = list(query_terms)
        self.anchor_chunk_id = anchor_chunk_id
        self.anchor_document_id = anchor_document_id
        self.cutoff = cutoff


@dataclass(frozen=True)
class CoverageResult:
    ticker: str
    ok: bool
    eligible_count: int
    filing_v3_count: int
    future_count: int
    reason: str | None = None


@dataclass(frozen=True)
class SmokeProbeResult:
    ticker: str
    ok: bool
    query_terms: list[str]
    anchor_chunk_id: str | None
    anchor_document_id: str | None
    cutoff: str | None
    hit_mode: str | None
    mode_served: str | None
    failure_reason: str | None
    is_quality_metric: bool = False


def _require_hex64(value: str, label: str) -> str:
    if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def _require_utc_second(value: str, label: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        raise ValueError(f"{label} must use YYYY-MM-DDTHH:MM:SSZ") from None
    if parsed.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ValueError(f"{label} must be canonical UTC")
    return value


def build_lexical_smoke_query(content_text: str) -> list[str]:
    text = unicodedata.normalize("NFC", content_text or "").lower()
    terms: list[str] = []
    seen: set[str] = set()
    for match in _WORD_RE.finditer(text):
        term = match.group(0)
        if term in SMOKE_STOPWORDS_V1 or term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) == 8:
            break
    if not terms:
        raise ValueError("no non-stopword terms for lexical smoke query")
    return terms


def fts5_and_query(terms: Sequence[str]) -> str:
    return " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _require_current_corpus_binding(
    conn: sqlite3.Connection,
    *,
    snapshot_id: str,
    corpus_manifest_id: str,
) -> None:
    try:
        row = conn.execute(
            """SELECT manifest_json, is_current FROM corpus_manifest
               WHERE manifest_id=?""",
            (corpus_manifest_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise ValueError("probe corpus_manifest table missing or invalid") from exc
    if row is None:
        raise ValueError("probe corpus_manifest_id not found")
    if int(row[1] or 0) != 1:
        raise ValueError("probe corpus manifest is_current must be 1")
    try:
        manifest = json.loads(row[0] or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("probe corpus manifest_json invalid") from exc
    if manifest.get("certified_snapshot_identity") != snapshot_id:
        raise ValueError("probe corpus manifest certified_snapshot_identity mismatch")
    current = conn.execute(
        "SELECT manifest_id FROM corpus_manifest WHERE is_current=1"
    ).fetchall()
    if current != [(corpus_manifest_id,)]:
        raise ValueError("probe requires exactly one current corpus manifest")
    lexical = conn.execute(
        """SELECT corpus_manifest_id, mode_served FROM lexical_index_state
           WHERE singleton_id=1"""
    ).fetchone()
    if lexical is None:
        raise ValueError("lexical_index_state missing")
    if lexical[0] != corpus_manifest_id:
        raise ValueError("lexical index corpus manifest stale")
    if lexical[1] != "fts5":
        raise ValueError("lexical index mode_served must be fts5")


def load_and_verify_postbuild_readiness_report(
    conn: sqlite3.Connection,
    path: Path,
    *,
    expected_universe_manifest_id: str,
    expected_snapshot_id: str,
    expected_corpus_manifest_id: str,
) -> dict[str, Any]:
    """Authenticate post-corpus readiness against its artifact and live DB."""
    path = Path(path)
    if not path.is_file():
        raise ValueError("postbuild readiness report missing")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid postbuild readiness report") from exc
    readiness = report.get("postbuild_readiness") if isinstance(report, dict) else None
    if not isinstance(readiness, dict):
        raise ValueError("postbuild readiness report missing identity binding")
    if readiness.get("postbuild_evidence_ready") is not True:
        raise ValueError("postbuild_evidence_ready must be true")
    if readiness.get("schema_version") != "pre_b6_postbuild_readiness_v1":
        raise ValueError("postbuild readiness schema mismatch")
    for label, expected in (
        ("universe_manifest_id", expected_universe_manifest_id),
        ("snapshot_id", expected_snapshot_id),
        ("corpus_manifest_id", expected_corpus_manifest_id),
        ("lexical_manifest_id", expected_corpus_manifest_id),
    ):
        if readiness.get(label) != expected:
            raise ValueError(f"postbuild readiness {label} mismatch")
    for label in (
        "inventory_id",
        "source_snapshot_id",
        "universe_manifest_id",
        "snapshot_id",
        "corpus_manifest_id",
        "lexical_manifest_id",
    ):
        _require_hex64(str(readiness.get(label) or ""), label)
    for label in (
        "b2o_terminal_run_id",
        "s1_terminal_run_id",
        "s2_terminal_run_id",
        "s4_terminal_run_id",
    ):
        if not isinstance(readiness.get(label), str) or not readiness[label]:
            raise ValueError(f"postbuild readiness missing {label}")
    stored_id = readiness.get("postbuild_readiness_id")
    identity_body = dict(readiness)
    identity_body.pop("postbuild_readiness_id", None)
    if stored_id != sha256_identity(identity_body):
        raise ValueError("postbuild readiness identity mismatch")

    binding = ((report.get("b2o_readiness") or {}).get("readiness_binding") or {})
    if not binding:
        raise ValueError("postbuild readiness report missing readiness_binding")
    for label in (
        "b2o_terminal_run_id",
        "s1_terminal_run_id",
        "s2_terminal_run_id",
        "s4_terminal_run_id",
        "inventory_id",
        "universe_manifest_id",
        "source_snapshot_id",
    ):
        if binding.get(label) != readiness.get(label):
            raise ValueError(f"postbuild readiness binding {label} mismatch")

    _require_current_corpus_binding(
        conn,
        snapshot_id=expected_snapshot_id,
        corpus_manifest_id=expected_corpus_manifest_id,
    )
    document_ids = readiness.get("mandatory_document_ids")
    if not isinstance(document_ids, list) or not document_ids:
        raise ValueError("postbuild readiness mandatory SEC document IDs missing")
    if len(document_ids) != len(set(document_ids)):
        raise ValueError("postbuild readiness mandatory SEC document IDs duplicate")
    if readiness.get("expected_mandatory_count") != len(document_ids):
        raise ValueError("postbuild readiness mandatory SEC document count mismatch")
    marks = ",".join("?" for _ in document_ids)
    chunks_relation = served_chunks_relation(conn)
    rows = conn.execute(
        f"""SELECT DISTINCT document_id FROM {chunks_relation}
            WHERE manifest_id=? AND chunk_profile_version='filing_v3'
              AND eligibility='eligible'
              AND status IN ('active','pending_embedding','embedded','metadata_only')
              AND document_id IN ({marks})""",
        [expected_corpus_manifest_id, *document_ids],
    ).fetchall()
    chunked_ids = {str(row[0]) for row in rows}
    missing = sorted(set(document_ids) - chunked_ids)
    if missing:
        raise ValueError(
            "postbuild readiness missing filing_v3 chunks for mandatory SEC document: "
            + missing[0]
        )
    if readiness.get("chunked_count") != len(document_ids):
        raise ValueError("postbuild readiness chunked_count mismatch")
    return readiness


def check_corpus_coverage_invariant(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    corpus_manifest_id: str,
    cutoff: str,
) -> CoverageResult:
    chunks_relation = served_chunks_relation(conn)
    status_marks = ",".join("?" for _ in SEARCHABLE_STATUSES)
    base_params: list[object] = [
        corpus_manifest_id,
        *SEARCHABLE_STATUSES,
        ticker,
    ]
    eligible = conn.execute(
        f"""SELECT COUNT(*) FROM {chunks_relation} c
            WHERE c.manifest_id=?
              AND c.status IN ({status_marks})
              AND c.eligibility='eligible'
              AND EXISTS (
                SELECT 1 FROM json_each(c.ticker_associations) t WHERE t.value=?
              )
              AND c.available_at<=?""",
        [*base_params, cutoff],
    ).fetchone()[0]
    filing_v3 = conn.execute(
        f"""SELECT COUNT(*) FROM {chunks_relation} c
            WHERE c.manifest_id=?
              AND c.status IN ({status_marks})
              AND c.eligibility='eligible'
              AND EXISTS (
                SELECT 1 FROM json_each(c.ticker_associations) t WHERE t.value=?
              )
              AND c.available_at<=?
              AND c.chunk_profile_version='filing_v3'""",
        [*base_params, cutoff],
    ).fetchone()[0]
    future = conn.execute(
        f"""SELECT COUNT(*) FROM {chunks_relation} c
            WHERE c.manifest_id=?
              AND c.status IN ({status_marks})
              AND c.eligibility='eligible'
              AND EXISTS (
                SELECT 1 FROM json_each(c.ticker_associations) t WHERE t.value=?
              )
              AND c.available_at>?""",
        [*base_params, cutoff],
    ).fetchone()[0]
    ok = int(eligible) >= 1 and int(filing_v3) >= 1 and int(future) == 0
    reason = None
    if int(future):
        reason = "future_available_at"
    elif not int(eligible):
        reason = "missing_eligible_chunk"
    elif not int(filing_v3):
        reason = "missing_filing_v3"
    return CoverageResult(
        ticker=ticker,
        ok=ok,
        eligible_count=int(eligible),
        filing_v3_count=int(filing_v3),
        future_count=int(future),
        reason=reason,
    )


def run_lexical_smoke_probe(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    corpus_manifest_id: str,
    probe_cutoff: str,
) -> SmokeProbeResult:
    chunks_relation = served_chunks_relation(conn)
    row = conn.execute(
        f"""SELECT chunk_id, document_id, content_text, available_at
           FROM {chunks_relation}
           WHERE manifest_id=?
             AND chunk_profile_version='filing_v3'
             AND eligibility='eligible'
             AND status IN ('active','pending_embedding','embedded','metadata_only')
             AND available_at<=?
             AND EXISTS (
               SELECT 1 FROM json_each(ticker_associations) t WHERE t.value=?
             )
           ORDER BY chunk_id
           LIMIT 1""",
        (corpus_manifest_id, probe_cutoff, ticker),
    ).fetchone()
    if row is None:
        raise LexicalSmokeError(
            f"no filing_v3 anchor for {ticker}", reason="no_anchor"
        )
    anchor_id, document_id, content, anchor_cutoff = row
    terms = build_lexical_smoke_query(content)

    from catalyst_data.retrieval.fts5 import retrieve_lexical
    from catalyst_data.retrieval.result import RetrievalContractError

    try:
        result_set = retrieve_lexical(
            conn,
            " ".join(terms),
            ticker=ticker,
            cutoff=anchor_cutoff,
            requested_manifest_id=corpus_manifest_id,
            top_k=8,
            candidate_depth=20,
        )
    except RetrievalContractError as exc:
        raise LexicalSmokeError(
            f"lexical retrieval contract failed: {exc}",
            reason="retrieval_contract",
            query_terms=terms,
            anchor_chunk_id=anchor_id,
            anchor_document_id=document_id,
            cutoff=anchor_cutoff,
        ) from exc
    mode = result_set.mode_served
    if mode != "fts5":
        raise LexicalSmokeError(
            f"lexical smoke requires fts5, got {mode}",
            reason=result_set.fallback_reason or "not_fts5",
            mode_served=mode,
            query_terms=terms,
            anchor_chunk_id=anchor_id,
            anchor_document_id=document_id,
            cutoff=anchor_cutoff,
        )
    hit_mode = None
    for hit in result_set.results[:8]:
        if hit.chunk_id == anchor_id:
            hit_mode = "anchor"
            break
        if hit.document_id == document_id:
            hit_mode = "sibling"
            break
    if hit_mode is None:
        raise LexicalSmokeError(
            "no anchor or sibling hit in FTS5 top-8",
            reason="no_hit",
            mode_served=mode,
            query_terms=terms,
            anchor_chunk_id=anchor_id,
            anchor_document_id=document_id,
            cutoff=anchor_cutoff,
        )
    return SmokeProbeResult(
        ticker=ticker,
        ok=True,
        query_terms=terms,
        anchor_chunk_id=anchor_id,
        anchor_document_id=document_id,
        cutoff=anchor_cutoff,
        hit_mode=hit_mode,
        mode_served=mode,
        failure_reason=None,
    )


def _report_identity(body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": body["schema_version"],
        "policy_revision": body["policy_revision"],
        "universe_manifest_id": body["universe_manifest_id"],
        "snapshot_id": body["snapshot_id"],
        "corpus_manifest_id": body["corpus_manifest_id"],
        "postbuild_readiness_id": body["postbuild_readiness_id"],
        "ordered_tickers": body["ordered_tickers"],
        "probe_cutoff": body["probe_cutoff"],
        "coverage_results": body["coverage_results"],
        "lexical_results": body["lexical_results"],
        "coverage_pass_count": body["coverage_pass_count"],
        "lexical_pass_count": body["lexical_pass_count"],
        "overall_pass": body["overall_pass"],
    }


def compute_probe_report_id(body: Mapping[str, Any]) -> str:
    return sha256_identity(_report_identity(body))


def _atomic_write_json(path: Path, body: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(body, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def evaluate_pre_b6_probe_report(
    conn: sqlite3.Connection,
    *,
    universe_manifest_id: str,
    snapshot_id: str,
    corpus_manifest_id: str,
    ordered_tickers: Sequence[str],
    probe_cutoff: str,
    postbuild_readiness_report_path: Path,
) -> dict[str, Any]:
    """Recompute the deterministic probe report from the current DB, read-only."""
    _require_hex64(universe_manifest_id, "universe_manifest_id")
    _require_hex64(snapshot_id, "snapshot_id")
    _require_hex64(corpus_manifest_id, "corpus_manifest_id")
    _require_utc_second(probe_cutoff, "probe_cutoff")
    tickers = list(ordered_tickers)
    if tickers != list(RATIFIED_TICKERS):
        raise ValueError("Pre-B6 probes require exact ordered ratified 40 tickers")
    _require_current_corpus_binding(
        conn,
        snapshot_id=snapshot_id,
        corpus_manifest_id=corpus_manifest_id,
    )
    postbuild = load_and_verify_postbuild_readiness_report(
        conn,
        postbuild_readiness_report_path,
        expected_universe_manifest_id=universe_manifest_id,
        expected_snapshot_id=snapshot_id,
        expected_corpus_manifest_id=corpus_manifest_id,
    )

    coverage_results: list[dict[str, Any]] = []
    lexical_results: list[dict[str, Any]] = []
    for ticker in tickers:
        coverage = check_corpus_coverage_invariant(
            conn,
            ticker=ticker,
            corpus_manifest_id=corpus_manifest_id,
            cutoff=probe_cutoff,
        )
        coverage_results.append(asdict(coverage))
        try:
            lexical = run_lexical_smoke_probe(
                conn,
                ticker=ticker,
                corpus_manifest_id=corpus_manifest_id,
                probe_cutoff=probe_cutoff,
            )
        except (LexicalSmokeError, ValueError) as exc:
            lexical = SmokeProbeResult(
                ticker=ticker,
                ok=False,
                query_terms=list(getattr(exc, "query_terms", [])),
                anchor_chunk_id=getattr(exc, "anchor_chunk_id", None),
                anchor_document_id=getattr(exc, "anchor_document_id", None),
                cutoff=getattr(exc, "cutoff", None),
                hit_mode=None,
                mode_served=getattr(exc, "mode_served", None),
                failure_reason=getattr(exc, "reason", None) or str(exc),
            )
        lexical_results.append(asdict(lexical))

    coverage_pass_count = sum(1 for result in coverage_results if result["ok"])
    lexical_pass_count = sum(1 for result in lexical_results if result["ok"])
    body: dict[str, Any] = {
        "schema_version": PROBE_REPORT_SCHEMA_VERSION,
        "policy_revision": PROBE_POLICY_REVISION,
        "universe_manifest_id": universe_manifest_id,
        "snapshot_id": snapshot_id,
        "corpus_manifest_id": corpus_manifest_id,
        "postbuild_readiness_id": postbuild["postbuild_readiness_id"],
        "ordered_tickers": tickers,
        "probe_cutoff": probe_cutoff,
        "coverage_results": coverage_results,
        "lexical_results": lexical_results,
        "coverage_pass_count": coverage_pass_count,
        "lexical_pass_count": lexical_pass_count,
        "overall_pass": coverage_pass_count == 40 and lexical_pass_count == 40,
    }
    body["probe_report_id"] = compute_probe_report_id(body)
    return body


def run_pre_b6_probe_gates(
    conn: sqlite3.Connection,
    *,
    universe_manifest_id: str,
    snapshot_id: str,
    corpus_manifest_id: str,
    ordered_tickers: Sequence[str],
    probe_cutoff: str,
    output_path: Path,
    postbuild_readiness_report_path: Path,
) -> dict[str, Any]:
    body = evaluate_pre_b6_probe_report(
        conn,
        universe_manifest_id=universe_manifest_id,
        snapshot_id=snapshot_id,
        corpus_manifest_id=corpus_manifest_id,
        ordered_tickers=ordered_tickers,
        probe_cutoff=probe_cutoff,
        postbuild_readiness_report_path=postbuild_readiness_report_path,
    )
    _atomic_write_json(Path(output_path), body)
    if not body["overall_pass"]:
        raise PreB6ProbeGateError(
            "Pre-B6 promotion requires 40/40 corpus and 40/40 lexical probes",
            report=body,
        )
    return body


def verify_probe_report_against_db(
    conn: sqlite3.Connection,
    report: Mapping[str, Any],
    *,
    expected_universe_manifest_id: str,
    expected_snapshot_id: str,
    expected_corpus_manifest_id: str,
    expected_probe_cutoff: str,
    postbuild_readiness_report_path: Path,
) -> dict[str, Any]:
    """Authenticate a structurally valid report against current DB evidence."""
    if report.get("universe_manifest_id") != expected_universe_manifest_id:
        raise ValueError("DB-backed probe report universe mismatch")
    if report.get("snapshot_id") != expected_snapshot_id:
        raise ValueError("DB-backed probe report snapshot mismatch")
    if report.get("corpus_manifest_id") != expected_corpus_manifest_id:
        raise ValueError("DB-backed probe report corpus mismatch")
    if report.get("probe_cutoff") != expected_probe_cutoff:
        raise ValueError("DB-backed probe report cutoff mismatch")
    postbuild = load_and_verify_postbuild_readiness_report(
        conn,
        postbuild_readiness_report_path,
        expected_universe_manifest_id=expected_universe_manifest_id,
        expected_snapshot_id=expected_snapshot_id,
        expected_corpus_manifest_id=expected_corpus_manifest_id,
    )
    if report.get("postbuild_readiness_id") != postbuild["postbuild_readiness_id"]:
        raise ValueError("DB-backed probe report postbuild readiness mismatch")
    try:
        recomputed = evaluate_pre_b6_probe_report(
            conn,
            universe_manifest_id=expected_universe_manifest_id,
            snapshot_id=expected_snapshot_id,
            corpus_manifest_id=expected_corpus_manifest_id,
            ordered_tickers=list(RATIFIED_TICKERS),
            probe_cutoff=expected_probe_cutoff,
            postbuild_readiness_report_path=postbuild_readiness_report_path,
        )
    except (sqlite3.Error, ValueError) as exc:
        raise ValueError(
            f"DB-backed probe report verification failed: {exc}"
        ) from exc
    if _report_identity(report) != _report_identity(recomputed):
        raise ValueError("DB-backed probe report evidence mismatch")
    if report.get("probe_report_id") != recomputed["probe_report_id"]:
        raise ValueError("DB-backed probe report identity mismatch")
    if recomputed["overall_pass"] is not True:
        raise ValueError("DB-backed probe report is not certified 40/40")
    return recomputed


def load_and_verify_probe_report(
    path: Path,
    *,
    expected_snapshot_id: str,
    expected_corpus_manifest_id: str,
    expected_universe_manifest_id: str | None = None,
    expected_postbuild_readiness_id: str,
) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise ValueError("certified Pre-B6 probe report missing")
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Pre-B6 probe report") from exc
    if not isinstance(body, dict):
        raise ValueError("Pre-B6 probe report must be an object")
    if body.get("schema_version") != PROBE_REPORT_SCHEMA_VERSION:
        raise ValueError("Pre-B6 probe report schema mismatch")
    if body.get("policy_revision") != PROBE_POLICY_REVISION:
        raise ValueError("Pre-B6 probe report policy mismatch")
    if body.get("snapshot_id") != expected_snapshot_id:
        raise ValueError("Pre-B6 probe report snapshot mismatch")
    if body.get("corpus_manifest_id") != expected_corpus_manifest_id:
        raise ValueError("Pre-B6 probe report corpus mismatch")
    if body.get("postbuild_readiness_id") != expected_postbuild_readiness_id:
        raise ValueError("Pre-B6 probe report postbuild readiness mismatch")
    if (
        expected_universe_manifest_id is not None
        and body.get("universe_manifest_id") != expected_universe_manifest_id
    ):
        raise ValueError("Pre-B6 probe report universe mismatch")
    if body.get("ordered_tickers") != list(RATIFIED_TICKERS):
        raise ValueError("Pre-B6 probe report ticker universe mismatch")
    coverage = body.get("coverage_results")
    lexical = body.get("lexical_results")
    if not isinstance(coverage, list) or not isinstance(lexical, list):
        raise ValueError("Pre-B6 probe report evidence missing")
    if [row.get("ticker") for row in coverage] != list(RATIFIED_TICKERS):
        raise ValueError("Pre-B6 coverage evidence order mismatch")
    if [row.get("ticker") for row in lexical] != list(RATIFIED_TICKERS):
        raise ValueError("Pre-B6 lexical evidence order mismatch")
    if (
        body.get("coverage_pass_count") != 40
        or body.get("lexical_pass_count") != 40
        or body.get("overall_pass") is not True
        or any(row.get("ok") is not True for row in coverage)
        or any(
            row.get("ok") is not True or row.get("mode_served") != "fts5"
            for row in lexical
        )
    ):
        raise ValueError("Pre-B6 probe report is not certified 40/40")
    expected_id = compute_probe_report_id(body)
    if body.get("probe_report_id") != expected_id:
        raise ValueError("Pre-B6 probe report identity mismatch")
    return body


__all__ = [
    "CoverageResult",
    "LexicalSmokeError",
    "PreB6ProbeGateError",
    "PROBE_POLICY_REVISION",
    "SMOKE_STOPWORDS_V1",
    "SmokeProbeResult",
    "build_lexical_smoke_query",
    "check_corpus_coverage_invariant",
    "compute_probe_report_id",
    "evaluate_pre_b6_probe_report",
    "fts5_and_query",
    "load_and_verify_probe_report",
    "load_and_verify_postbuild_readiness_report",
    "run_lexical_smoke_probe",
    "run_pre_b6_probe_gates",
    "verify_probe_report_against_db",
]
