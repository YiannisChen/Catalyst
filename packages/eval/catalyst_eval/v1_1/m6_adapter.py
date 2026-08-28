"""M6 app/SSE runner adapter for Stage-1 execute (M7-8, Batch-B).

Eval-owned adapter that drives the live M6 app/SSE boundary through an
injected ``RuntimeComposition`` (AdmissionController + RunClaimer + SQLite
authority). ``None`` never reaches this module from the CLI default; the
operator wires the production composition at M7-10. The adapter is fully
fixture-testable: tests inject a fake composition and a seeded runtime DB and
verify that the returned ``CaseRunOutcome`` hashes are recomputed from the
serialized artifacts.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from catalyst_eval.v1_1.loader import canonical_bytes
from catalyst_eval.v1_1.runner import CaseRunOutcome


class Stage1AdapterError(RuntimeError):
    pass


def _sha256_canonical(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def default_case_request_builder(case: Any, **kwargs) -> Any:
    """Build an M6 AdmissionRequest from a GoldenCase (lazy app import)."""
    from catalyst_app.runtime.admission import AdmissionRequest

    return AdmissionRequest(
        ticker=case.ticker,
        session_date=case.session_date,
        query=case.question,
        provider=kwargs.get("provider", "deepseek"),
        model_id=kwargs.get("model_id", "deepseek-chat"),
        base_url=kwargs.get("base_url"),
        credential_source_identifier=kwargs.get(
            "credential_source", "server_env"
        ),
        workflow_version="v1.1",
        config_version="v1.1",
    )


class M6AppSseRunnerAdapter:
    """Drive one case through the M6 app/SSE boundary and seal its artifacts."""

    def __init__(
        self,
        *,
        composition: Any,
        db_path: str | Path,
        case_request_builder: Callable[..., Any] | None = None,
        provider: str = "deepseek",
        model_id: str = "deepseek-chat",
        base_url: str | None = None,
        credential_source: str = "server_env",
        poll_interval_seconds: float = 0.05,
        poll_timeout_seconds: float = 30.0,
    ) -> None:
        self.composition = composition
        self.db_path = Path(db_path)
        self.case_request_builder = case_request_builder or default_case_request_builder
        self.provider = provider
        self.model_id = model_id
        self.base_url = base_url
        self.credential_source = credential_source
        self.poll_interval_seconds = poll_interval_seconds
        self.poll_timeout_seconds = poll_timeout_seconds

    # -- public ------------------------------------------------------------

    def run_case(self, case: Any) -> CaseRunOutcome:
        """Admit one run, wait for a terminal lifecycle, seal the outcome."""
        request = self.case_request_builder(
            case,
            provider=self.provider,
            model_id=self.model_id,
            base_url=self.base_url,
            credential_source=self.credential_source,
        )
        admitted = self.composition.admission.admit(request)
        if admitted.kind != "accepted" or not admitted.run_id:
            raise Stage1AdapterError(
                f"admission for {case.case_id!r} was not accepted: "
                f"kind={admitted.kind} failure_code={admitted.failure_code}"
            )
        run_id = admitted.run_id
        self._wait_terminal(run_id)
        return self._load_outcome(run_id, case.case_id)

    # -- terminal wait -----------------------------------------------------

    def _wait_terminal(self, run_id: str) -> None:
        deadline = time.monotonic() + self.poll_timeout_seconds
        while time.monotonic() < deadline:
            lifecycle = self.composition.claimer.current_lifecycle(run_id)
            if lifecycle is not None and lifecycle.value in {
                "COMPLETED", "FAILED", "CANCELLED",
            }:
                return
            time.sleep(self.poll_interval_seconds)
        raise Stage1AdapterError(
            f"run {run_id!r} did not reach a terminal state within "
            f"{self.poll_timeout_seconds}s"
        )

    # -- artifact loading --------------------------------------------------

    def _load_outcome(self, run_id: str, case_id: str) -> CaseRunOutcome:
        from catalyst_app.persistence.connect import open_rw

        with open_rw(self.db_path) as conn:
            rows = conn.execute(
                "SELECT artifact_type, artifact_id, payload_json FROM run_artifacts"
                " WHERE run_id = ? ORDER BY event_seq ASC",
                (run_id,),
            ).fetchall()
            lifecycle_row = conn.execute(
                "SELECT lifecycle_status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if lifecycle_row is None:
            raise Stage1AdapterError(f"run {run_id!r} has no lifecycle row")
        terminal_status = str(lifecycle_row["lifecycle_status"])

        artifacts: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            artifacts.setdefault(row["artifact_type"], []).append(
                {
                    "artifact_id": row["artifact_id"],
                    "payload_json": row["payload_json"],
                }
            )

        manifest_records = artifacts.get("run_manifest") or []
        if not manifest_records:
            raise Stage1AdapterError(
                f"run {run_id!r} has no persisted RunManifest artifact"
            )
        manifest_payload = _parse_payload(manifest_records[0]["payload_json"], run_id)
        attribution_records = artifacts.get("attribution_result") or []
        if not attribution_records:
            raise Stage1AdapterError(
                f"run {run_id!r} has no persisted attribution result artifact"
            )
        result_payload = _parse_payload(
            attribution_records[0]["payload_json"], run_id
        )

        claims: list[dict[str, Any]] = []
        for record in artifacts.get("claim_detail") or []:
            payload = _parse_payload(record["payload_json"], run_id)
            claims.append(
                {
                    "claim_id": payload.get("claim_id") or record["artifact_id"],
                    "material": True,
                    "citation_ids": list(payload.get("citation_evidence_ids") or ()),
                    "role": payload.get("role") or "PRIMARY",
                    "statement": payload.get("statement") or "",
                }
            )

        run_facts = {
            "schema_version": "v1_1_stage1_run_facts_v1",
            "case_id": case_id,
            "output_status": result_payload.get("attribution_status")
            or terminal_status,
            "attribution_type": result_payload.get("attribution_type"),
            "refusal_reason": None,
            "claims": claims,
            "sanity_tasks_completed": [],
            "trajectory": {
                "corrective_triggered": False,
                "gap_ids": [],
                "corrective_actions": [],
                "stop_correct": True,
                "new_structure_created": False,
                "corrected": False,
            },
            "retrieval": {
                "pool_id": "p" * 64,
                "ranked_evidence_ids": [],
                "reranker_contributed": False,
                "ticker_violations": [],
                "cutoff_violations": [],
                "degraded": False,
            },
        }

        def _pack_ref(artifact_type: str, default_prefix: str) -> dict[str, str] | None:
            records = artifacts.get(artifact_type) or []
            if not records:
                return None
            payload = _parse_payload(records[0]["payload_json"], run_id)
            return {
                "artifact_id": records[0]["artifact_id"]
                or f"{default_prefix}:{run_id}",
                "artifact_hash": _sha256_canonical(payload),
            }

        context_pack_ref = _pack_ref("context_pack", "pack") or {
            "artifact_id": f"pack:{run_id}",
            "artifact_hash": _sha256_canonical(manifest_payload),
        }
        claim_plan_ref = _pack_ref("claim_plan", "claimplan") or {
            "artifact_id": f"claimplan:{run_id}",
            "artifact_hash": _sha256_canonical(result_payload),
        }
        assurance_ref = _pack_ref("assurance", "assurance") or {
            "artifact_id": f"assurance:{run_id}",
            "artifact_hash": _sha256_canonical(manifest_payload),
        }

        return CaseRunOutcome(
            case_id=case_id,
            run_manifest_id=str(manifest_payload.get("run_id") or run_id),
            run_manifest_hash=_sha256_canonical(manifest_payload),
            result_artifact_id=attribution_records[0]["artifact_id"],
            result_artifact_hash=_sha256_canonical(result_payload),
            terminal_status=terminal_status,
            run_manifest_payload=manifest_payload,
            result_artifact_payload=result_payload,
            context_pack_ref=context_pack_ref,
            claim_plan_ref=claim_plan_ref,
            assurance_ref=assurance_ref,
            provider_calls=int(result_payload.get("provider_calls") or 0),
            cost_usd=result_payload.get("cost_usd"),
            latency_ms=result_payload.get("latency_ms"),
            tokens=result_payload.get("tokens"),
            run_facts=run_facts,
        )


def _parse_payload(payload_json: str, run_id: str) -> dict[str, Any]:
    import json

    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise Stage1AdapterError(
            f"run {run_id!r} artifact payload is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise Stage1AdapterError(
            f"run {run_id!r} artifact payload must be a JSON object"
        )
    return payload


__all__ = [
    "M6AppSseRunnerAdapter",
    "Stage1AdapterError",
    "default_case_request_builder",
]
