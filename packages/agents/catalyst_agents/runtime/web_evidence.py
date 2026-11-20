"""Run-scoped union of cached Tavily articles with local lexical evidence."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import Any

# Production callers pass the research stage's monotonic deadline. Direct callers that
# omit it still get a bounded wait rather than treating a later call as the leader.
_DEFAULT_SELECTION_WAIT_SECONDS = 30.0


def _sentences(text: str) -> set[str]:
    return {" ".join(part.casefold().split()) for part in re.split(r"(?<=[.!?])\s+", text or "") if len(part.strip()) > 35}


def _sentence_fingerprints(text: str) -> set[str]:
    return {hashlib.sha1(sentence.encode()).hexdigest()[:10] for sentence in _sentences(text)}


class WebEvidenceRetriever:
    """Add frozen discovery v2 and CNBC roundup rows to stage-two retrieval."""

    supports_research_submission_order = True

    def __init__(self, base: Any, *, mode: str = "discovery_v2") -> None:
        if mode != "discovery_v2":
            raise ValueError("unsupported web evidence runtime mode")
        self.base = base
        self.mode = mode
        self._lock = threading.RLock()
        self._offered: dict[str, dict[str, Any]] = {}
        self._used: dict[str, dict[str, Any]] = {}
        self._evidence_rows: dict[str, str] = {}
        self._selected_rows: list[dict[str, Any]] | None = None
        self._selection_ready = threading.Condition(self._lock)
        self._status = "not_available"
        self._extra_rows: list[dict[str, Any]] = []

    def add_rows(self, rows: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> None:
        """Add investigator rows (search results, SEC exhibits); call before the first retrieve."""
        with self._lock:
            self._extra_rows.extend(dict(row) for row in rows)

    def retrieve(self, query: str, *, research_submission_index: int | None = None,
                 research_deadline_monotonic: float | None = None, **kwargs):
        local = self.base.retrieve(query, **kwargs)
        return self._merge_web(
            query, tuple(local), kwargs,
            research_submission_index=research_submission_index,
            research_deadline_monotonic=research_deadline_monotonic,
        )

    def retrieve_with_observations(self, query: str, *,
                                   research_submission_index: int | None = None,
                                   research_deadline_monotonic: float | None = None,
                                   **kwargs):
        """Preserve the base retriever's measured observation at the agent seam."""
        retrieve_observed = getattr(self.base, "retrieve_with_observations", None)
        if callable(retrieve_observed):
            local, observation = retrieve_observed(query, **kwargs)
        else:
            from catalyst_agents.runtime.retrieval_adapter import AgentRetrieverAdapter

            local, observation = AgentRetrieverAdapter(self.base).retrieve_with_observations(
                query, **kwargs
            )
        return self._merge_web(
            query, local, kwargs,
            research_submission_index=research_submission_index,
            research_deadline_monotonic=research_deadline_monotonic,
        ), observation

    def _merge_web(self, query: str, local: tuple[Any, ...], kwargs: dict[str, Any],
                   *, research_submission_index: int | None = None,
                   research_deadline_monotonic: float | None = None):
        ticker = kwargs["ticker"].upper()
        temporal = kwargs.get("temporal_identity")
        session_date = temporal.session_date if temporal is not None else ""
        if not session_date:
            return local
        with self._lock:
            from catalyst_data.canonical.temporal import utc_iso_z
            from catalyst_data.web_evidence import (
                cached_discovery_v2_rows,
                cached_roundup_segments,
            )

            rows = [{**row, "query_kind": row["scope_key"]} for row in
                    cached_discovery_v2_rows(ticker, session_date)]
            # Investigator rows go after cached rows so off-target rows cannot displace them.
            seen = {str(row["canonical_url"]).rstrip("/").casefold() for row in rows}
            rows.extend(row for row in self._extra_rows
                        if str(row["canonical_url"]).rstrip("/").casefold() not in seen)
            start_at, end_at = temporal.publication_window()
            segments = cached_roundup_segments(
                ticker, session_date, start_at=utc_iso_z(start_at), end_at=utc_iso_z(end_at))
            rows.extend({
                    **row, "query_kind": "roundup", "body_text": row["segment_text"],
                    "canonical_url": f'{row["canonical_url"]}#segment-{row["segment_index"]}',
                    "html_sha256": row["page_sha256"], "publisher": "CNBC",
                    "publisher_family": "cnbc.com", "roundup_kind": row["roundup_kind"],
                } for row in segments)
            for row in rows:
                row_key = f"{row['query_kind']}:{row['canonical_url']}:{row.get('html_sha256') or ''}"
                self._offered[row_key] = {"query_kind": row["query_kind"], "canonical_url": row["canonical_url"],
                                          "html_sha256": row.get("html_sha256"),
                                          "body_source": row.get("body_source", "fetch"),
                                          **({"roundup_kind": row["roundup_kind"],
                                              "segment_index": row["segment_index"], "publisher": "CNBC"}
                                             if row.get("roundup_kind") else {})}
            local_urls = {str(item.canonical_url or "").rstrip("/").casefold() for item in local}
            local_sentences = set().union(*(_sentence_fingerprints(item.content_text) for item in local)) if local else set()
            selected_rows = self._selected_rows_for(
                ticker, rows, local_urls, local_sentences,
                research_submission_index=research_submission_index,
                research_deadline_monotonic=research_deadline_monotonic,
            )
            web_items = []
            for row in selected_rows:
                row_key = f"{row['query_kind']}:{row['canonical_url']}:{row.get('html_sha256') or ''}"
                canonical = str(row["canonical_url"]).rstrip("/").casefold()
                if canonical in local_urls or (_sentence_fingerprints(row["body_text"]) & local_sentences):
                    continue
                item = self._evidence(row, kwargs, local)
                web_items.append(item)
                self._evidence_rows[item.chunk_id] = row_key
            if rows:
                self._status = "available"
            return tuple(local) + tuple(web_items)

    def _selected_rows_for(self, ticker: str, rows: list[dict[str, Any]], local_urls: set[str],
                           local_sentences: set[str], *,
                           research_submission_index: int | None,
                           research_deadline_monotonic: float | None) -> list[dict[str, Any]]:
        """Return the run-scoped selection, seeded only by the first submitted caller."""
        if self._selected_rows is not None:
            return self._selected_rows
        if research_submission_index is None or research_submission_index == 0:
            self._selected_rows = self._select_rows(ticker, rows, local_urls, local_sentences)
            self._selection_ready.notify_all()
            return self._selected_rows
        if research_submission_index < 0:
            raise ValueError("research_submission_index must be non-negative")
        wait_deadline = research_deadline_monotonic
        if wait_deadline is None:
            wait_deadline = time.monotonic() + _DEFAULT_SELECTION_WAIT_SECONDS
        while self._selected_rows is None:
            remaining = wait_deadline - time.monotonic()
            if remaining <= 0:
                # Fail closed: the first-submitted local evidence is unavailable, so no
                # later caller may substitute its own and make the outcome order-dependent.
                self._selected_rows = []
                self._selection_ready.notify_all()
                break
            self._selection_ready.wait(timeout=remaining)
        return self._selected_rows

    def finish_research_submission(self, research_submission_index: int) -> None:
        """Let waiters fail closed if the first submitted task ended without seeding."""
        with self._selection_ready:
            if research_submission_index == 0 and self._selected_rows is None:
                self._selected_rows = []
                self._selection_ready.notify_all()

    def _select_rows(self, ticker: str, rows: list[dict[str, Any]], local_urls: set[str],
                     local_sentences: set[str]) -> list[dict[str, Any]]:
        """Deduplicate in frozen cache order; roundup segments follow discovery rows."""
        selected = []
        selected_urls = set()
        selected_fingerprints = set()
        for row in rows:
            canonical = str(row["canonical_url"]).rstrip("/").casefold()
            fingerprints = _sentence_fingerprints(row["body_text"])
            if (canonical in local_urls or canonical in selected_urls
                    or fingerprints & (local_sentences | selected_fingerprints)):
                continue
            selected.append(row)
            selected_urls.add(canonical)
            selected_fingerprints.update(fingerprints)
        return selected

    @staticmethod
    def _evidence(row: dict[str, Any], kwargs: dict[str, Any], local: tuple[Any, ...]):
        from datetime import datetime

        from catalyst_agents.attribution.provider import RetrievedEvidence

        digest = hashlib.sha256((row["canonical_url"] + (row.get("html_sha256") or "")).encode()).hexdigest()
        page_hash = row.get("page_sha256") or row.get("html_sha256") or digest
        page_document_id = "webdoc_" + page_hash[:32]
        timestamp = datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))
        from catalyst_data.web_evidence import web_source_class

        source_class = web_source_class(row["canonical_url"], row.get("source_class"))
        first = local[0] if local else None
        return RetrievedEvidence(
            # "ext_" not "web_": the analyst rewrote "web_<h>" as "web:<h>" by analogy
            # with local "web:<h>:web_body:NNNN" ids, failing pack-inventory checks.
            chunk_id="ext_" + digest[:32],
            document_id=page_document_id if row.get("roundup_kind") else "webdoc_" + digest[:32],
            content_text=row["body_text"], available_at=row["published_at"], source_class=source_class,
            ticker_associations=(kwargs["ticker"].upper(),),
            dedup_cluster_id="webpage_" + page_hash if row.get("roundup_kind") else None,
            cluster_first_available_at=row["published_at"],
            representative_document_id=page_document_id if row.get("roundup_kind") else "webdoc_" + digest[:32],
            is_novel=True, lexical_raw_score=None, lexical_rank=None,
            corpus_manifest_id=first.corpus_manifest_id if first else str(kwargs.get("requested_manifest_id") or "web_cache"),
            index_manifest_id=None, mode_requested="web_cache", mode_served="web_cache",
            is_degraded=False, fallback_reason=None, temporal_center_date=kwargs["temporal_identity"].session_date,
            canonical_asset_id="ext_" + (page_hash[:32] if row.get("roundup_kind") else digest[:32]),
            canonical_content_version_id=page_hash if row.get("roundup_kind") else (row.get("html_sha256") or digest),
            corpus_document_id=page_document_id if row.get("roundup_kind") else "webdoc_" + digest[:32],
            asset_type="NEWS", content_hash=page_hash if row.get("roundup_kind") else (row.get("html_sha256") or digest),
            material_capability="MATERIAL_CAPABLE", serving_status="body_candidate", temporal_precision="source_published_at",
            independence_group_id=row["publisher_family"],
            independence_status="KNOWN_GROUP" if source_class == "reported_news" else "UNKNOWN",
            canonical_url=row["canonical_url"], publisher=row["publisher"], provider="tavily",
            content_state="FULL_TEXT", eligible_at=timestamp, temporal_identity=kwargs["temporal_identity"],
            data_runtime_identity=first.data_runtime_identity if first else None, origin="web", parse_quality="ok",
            body_source=row.get("body_source", "fetch"),
        )

    def web_trace(self, *, used_evidence_ids: tuple[str, ...] = ()) -> dict[str, Any]:
        with self._lock:
            offered = [self._offered[key] for key in sorted(self._offered)]
            used_keys = {self._evidence_rows[evidence_id] for evidence_id in used_evidence_ids
                         if evidence_id in self._evidence_rows}
            used = [self._offered[key] for key in sorted(used_keys)]
            rows_hash = hashlib.sha256(json.dumps(used, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            # Retrieval merges web rows once per query; the last query's rows used to set the status, so a run that
            # was offered rows could report not_available (v2 display fix).
            status = "available" if offered else self._status
            return {"status": status, "rows_offered": len(offered), "rows_used": len(used),
                    "offered_rows": offered, "used_rows": used, "used_rows_sha256": rows_hash,
                    "credits_used": 0}
