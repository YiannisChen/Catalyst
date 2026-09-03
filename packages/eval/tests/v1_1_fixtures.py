"""Synthetic hidden-gold fixtures for V1.1 Stage-1 eval TDD (Batch B).

These fixtures are explicitly NOT the authoritative human-reviewed Stage-1
dataset (Q-011 gated; see M7-2). They exist so loader/manifest/metrics/
runner contracts are exercised offline with deterministic inputs. No
authoritative approval, reviewer identity, or audit decision is represented.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

ALLOWED_LEGACY_PARENTS = (
    "g006", "g013", "g017", "g024", "g041", "g007",
    "h001", "h004", "h005", "h007",
    "pre_b6", "pre_b6_attribution",
)

STAGE1_FIXTURE_CASE_IDS = (
    "v1f-001", "v1f-002", "v1f-003", "v1f-004", "v1f-005",
    "v1f-006", "v1f-007", "v1f-008", "v1f-009", "v1f-010",
    "v1f-011", "v1f-012",
)

# Stage-1 stratification contract (M7-2 Q-011 lock). challenge_family is a
# per-case scenario/challenge class from Frozen V1.1 §7.2, deliberately NOT
# inferred from accepted cause labels: an ABSTAIN case may be a MACRO/SECTOR
# challenge with an empty acceptable_cause_labels tuple.
CHALLENGE_FAMILY_VALUES = (
    "COMPANY_SPECIFIC",
    "MACRO",
    "SECTOR",
    "CONTINUATION",
    "DISTRACTOR",
    "QUIET",
    "ABSTENTION",
)
PRIMARY_EVIDENCE_KIND_VALUES = ("FULL_TEXT_BODY", "EIGHT_K_SHELL", "NONE")
MOVE_DIRECTION_VALUES = ("positive", "negative", "mixed", "unknown")

# challenge_family per synthetic case id. Macro/sector coverage is proven by
# these challenge-family strata, never by accepted cause labels alone.
_STAGE1_CHALLENGE_FAMILY = {
    "v1f-001": "COMPANY_SPECIFIC",
    "v1f-002": "SECTOR",
    "v1f-003": "MACRO",
    "v1f-004": "COMPANY_SPECIFIC",
    "v1f-005": "SECTOR",
    "v1f-006": "COMPANY_SPECIFIC",
    "v1f-007": "CONTINUATION",
    "v1f-008": "CONTINUATION",
    "v1f-009": "COMPANY_SPECIFIC",
    "v1f-010": "CONTINUATION",
    "v1f-011": "MACRO",
    "v1f-012": "SECTOR",
}

# Coverage-limited cases: local news serving is TITLE_ONLY/METADATA_ONLY
# (DATA-02); they stay non-scorable for news-attribution denominators.
COVERAGE_LIMITED_CASE_IDS = frozenset({"v1f-003", "v1f-012"})

# Truthful local 8-K-shell records: the shell is present but no substantive
# exhibit body is ingested; expected_primary_evidence stays empty.
EIGHT_K_SHELL_CASE_IDS = frozenset({"v1f-005"})

# Signed-session direction for rows whose acceptable_cause_labels are empty
# (ABSTAIN rows have no accepted labels by Stage-1 contract).
_STAGE1_MOVE_DIRECTION = {
    "v1f-011": "mixed",
}

def utc(iso: str) -> str:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).isoformat()


def make_case(
    *,
    case_id: str,
    ticker: str,
    session_date: str,
    cutoff: str,
    question: str,
    oracle_status: str,
    direction: str = "negative",
    cause_types: Iterable[str] = ("COMPANY_SPECIFIC_CATALYST",),
    labels: Iterable[str] | None = None,
    expected_primary_evidence: Iterable[str] = (),
    expected_refusal_reason: str | None = None,
    expected_attribution_type: str | None = "EVIDENCE_BACKED_CAUSAL",
    gap_reason_codes: Iterable[str] = (),
    corrective_recoverable: bool = False,
    corrective_required: bool = False,
    acceptable_initial_tasks: Iterable[str] = ("COMPANY_PRIMARY",),
    acceptable_corrective_actions: Iterable[dict[str, str]] = (),
    evidence_ids: Iterable[str] = (),
    notes: str | None = None,
) -> dict[str, Any]:
    """One synthetic GoldenCase row in dict form (JSONL-serializable)."""
    labels = tuple(labels) if labels is not None else tuple(
        f"fixture-label-{i}" for i in range(len(tuple(cause_types)))
    )
    evidence_ids = tuple(evidence_ids)
    return {
        "case_id": case_id,
        "ticker": ticker,
        "session_date": session_date,
        "cutoff": cutoff,
        "question": question,
        "oracle_status": oracle_status,
        "acceptable_cause_labels": [
            {
                "cause_type": cause_type,
                "label": label,
                "direction": direction,
                "materiality": "material",
            }
            for cause_type, label in zip(tuple(cause_types), labels)
        ],
        "evidence_judgments": [
            {
                "evidence_id": evidence_id,
                "canonical_asset_id": f"asset:{evidence_id}",
                "canonical_content_version_id": f"ver:{evidence_id}",
                "chunk_id": evidence_id,
                "fact_id": None,
                "role": "primary_support" if idx == 0 else "secondary_support",
                "support": True,
                "materiality": "material",
                "temporal_eligible": True,
                "independence_group": f"grp-{idx}",
                "rationale": f"fixture rationale for {evidence_id}",
                "annotator": None,
                "annotated_at": None,
            }
            for idx, evidence_id in enumerate(evidence_ids)
        ],
        "expected_primary_evidence": list(expected_primary_evidence),
        "expected_refusal_reason": expected_refusal_reason,
        "expected_attribution_type": expected_attribution_type,
        "expected_research_behavior": {
            "acceptable_initial_tasks": list(acceptable_initial_tasks),
            "expected_gap_reason_codes": list(gap_reason_codes),
            "acceptable_corrective_actions": list(acceptable_corrective_actions),
            "corrective_recoverable": corrective_recoverable,
            "corrective_required": corrective_required,
        },
        "notes": notes,
        "dataset_version": "1.1.0",
        "lineage": {
            "source": "fixture",
            "model_assisted_fields": [],
            "human_confirmed_fields": [],
            "annotated_at": "2026-08-19T00:00:00Z",
            "adjudication_state": "resolved",
        },
    }


def make_stage1_cases() -> list[dict[str, Any]]:
    """Twelve varied synthetic cases covering the Stage-1 strata.

    Natural strata (provisional natural adjudication, NOT an exact quota):
    SUFFICIENT 9 / PARTIAL 2 / ABSTAIN 1; six direct-primary FULL_TEXT_BODY
    cases, one truthful EIGHT_K_SHELL shell-only case, and five NONE/no-local-
    primary cases. Includes the c01-equivalent regression (SUFFICIENT with
    coverage_limited=true, no local material primary ID, empty
    expected_primary_evidence), at least one SUFFICIENT FULL_TEXT_BODY
    direct-primary case, COMPANY_SPECIFIC/MACRO/SECTOR challenge families,
    positive and negative moves, one recoverable and one multi-gap recoverable
    corrective case, and one ABSTAIN with empty labels plus a refusal reason.
    """
    specs = [
        dict(case_id="v1f-001", ticker="TSLA", session_date="2025-07-24",
             cutoff="2025-07-24T20:00:00Z", question="Why did TSLA fall after earnings?",
             oracle_status="SUFFICIENT", direction="negative",
             cause_types=("COMPANY_SPECIFIC_CATALYST",),
             evidence_ids=("fixture-ev-001", "fixture-ev-002"),
             expected_primary_evidence=("fixture-ev-001",),
             parent="g006"),
        dict(case_id="v1f-002", ticker="NVDA", session_date="2025-10-28",
             cutoff="2025-10-28T20:00:00Z", question="What drove NVDA higher?",
             oracle_status="SUFFICIENT", direction="positive",
             cause_types=("SECTOR_MOVE", "COMPANY_SPECIFIC_CATALYST"),
             labels=("fixture-sector", "fixture-company"),
             evidence_ids=("fixture-ev-003",),
             expected_primary_evidence=("fixture-ev-003",),
             parent="g013"),
        dict(case_id="v1f-003", ticker="AAPL", session_date="2025-06-12",
             cutoff="2025-06-12T20:00:00Z", question="Why did AAPL move this session?",
             oracle_status="SUFFICIENT", direction="positive",
             cause_types=("MACRO_EVENT",),
             labels=("fixture-tariff-relief",),
             expected_primary_evidence=(),
             expected_refusal_reason=None,
             expected_attribution_type="EVIDENCE_BACKED_CAUSAL",
             parent="h004",
             notes="c01-equivalent regression: public-world SUFFICIENT macro case with coverage-limited metadata-only news and no local material primary ID"),
        dict(case_id="v1f-004", ticker="MSFT", session_date="2025-09-15",
             cutoff="2025-09-15T20:00:00Z", question="What explains the MSFT decline?",
             oracle_status="SUFFICIENT", direction="negative",
             cause_types=("COMPANY_SPECIFIC_CATALYST", "MACRO_EVENT"),
             labels=("fixture-msft", "fixture-rates"),
             evidence_ids=("fixture-ev-004",),
             expected_primary_evidence=("fixture-ev-004",),
             parent="g017"),
        dict(case_id="v1f-005", ticker="GOOGL", session_date="2025-06-11",
             cutoff="2025-06-11T20:00:00Z", question="Why did GOOGL rise on this date?",
             oracle_status="SUFFICIENT", direction="positive",
             cause_types=("SECTOR_MOVE",),
             labels=("fixture-sector-rebound",),
             expected_primary_evidence=(),
             expected_refusal_reason=None,
             expected_attribution_type="EVIDENCE_BACKED_CAUSAL",
             parent="h005",
             notes="truthful EIGHT_K_SHELL local record: shell present, exhibit body absent, expected_primary_evidence empty"),
        dict(case_id="v1f-006", ticker="AMZN", session_date="2025-08-05",
             cutoff="2025-08-05T20:00:00Z", question="What moved AMZN today?",
             oracle_status="SUFFICIENT", direction="negative",
             cause_types=("COMPANY_SPECIFIC_CATALYST",),
             evidence_ids=("fixture-ev-005",),
             expected_primary_evidence=("fixture-ev-005",),
             parent="g024"),
        dict(case_id="v1f-007", ticker="META", session_date="2025-08-22",
             cutoff="2025-08-22T20:00:00Z", question="Why did META fall?",
             oracle_status="PARTIAL", direction="negative",
             cause_types=("CONTINUATION",),
             labels=("fixture-continuation",),
             evidence_ids=("fixture-ev-006",),
             expected_primary_evidence=(),
             parent="g007"),
        dict(case_id="v1f-008", ticker="TSLA", session_date="2025-08-22",
             cutoff="2025-08-22T20:00:00Z", question="Why did TSLA continue lower?",
             oracle_status="PARTIAL", direction="negative",
             cause_types=("CONTINUATION", "COMPANY_SPECIFIC_CATALYST"),
             labels=("fixture-cont-tsla", "fixture-tsla-news"),
             evidence_ids=("fixture-ev-007",),
             expected_primary_evidence=(),
             gap_reason_codes=("MISSING_PRIMARY_CONFIRMATION", "MISSING_INDEPENDENT_CORROBORATION"),
             corrective_recoverable=True,
             corrective_required=True,
             parent="g041"),
        dict(case_id="v1f-009", ticker="NFLX", session_date="2025-07-17",
             cutoff="2025-07-17T20:00:00Z", question="What drove NFLX this session?",
             oracle_status="SUFFICIENT", direction="positive",
             cause_types=("FUNDAMENTAL_REPRICING",),
             labels=("fixture-fundamental",),
             evidence_ids=("fixture-ev-008",),
             expected_primary_evidence=("fixture-ev-008",),
             parent="pre_b6"),
        dict(case_id="v1f-010", ticker="INTC", session_date="2025-08-01",
             cutoff="2025-08-01T20:00:00Z", question="Why did INTC sell off?",
             oracle_status="SUFFICIENT", direction="negative",
             cause_types=("REPORTING_OR_ANALYST_CONTINUATION",),
             labels=("fixture-analyst",),
             evidence_ids=(),
             expected_primary_evidence=(),
             expected_attribution_type="EVIDENCE_BACKED_CAUSAL",
             parent="g013",
             notes="SUFFICIENT with empty expected_primary_evidence and no local material primary; public-world analyst-continuation oracle"),
        dict(case_id="v1f-011", ticker="XOM", session_date="2025-09-10",
             cutoff="2025-09-10T20:00:00Z", question="Why did XOM move with oil?",
             oracle_status="ABSTAIN", direction="mixed",
             cause_types=(),
             expected_refusal_reason="insufficient_public_evidence",
             expected_attribution_type=None,
             parent="h007"),
        dict(case_id="v1f-012", ticker="COIN", session_date="2025-07-30",
             cutoff="2025-07-30T20:00:00Z", question="What moved COIN today?",
             oracle_status="SUFFICIENT", direction="positive",
             cause_types=("SECTOR_MOVE",),
             labels=("fixture-crypto",),
             evidence_ids=("fixture-ev-009",),
             expected_primary_evidence=("fixture-ev-009",),
             corrective_recoverable=False,
             corrective_required=False,
             parent="h001",
             notes="coverage limited news body (title-only); bound primary body is FULL_TEXT material evidence"),
    ]
    cases: list[dict[str, Any]] = []
    for spec in specs:
        case = make_case(**{k: v for k, v in spec.items() if k != "parent"})
        case["lineage"]["source"] = f"legacy:{spec['parent']}"
        cases.append(case)
    return cases


def make_stratification(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Synthetic stratification manifest for the fixture cases.

    Per-case fields follow the M7-2 contract: challenge_family is the
    scenario/challenge class (never derived from accepted cause labels);
    primary_evidence_kind is FULL_TEXT_BODY/NONE/EIGHT_K_SHELL where
    EIGHT_K_SHELL truthfully records a local 8-K shell whose exhibit body is
    absent (expected_primary_evidence stays empty); move_direction is the
    signed-session direction even when accepted labels are empty. Declared
    aggregate strata are recomputed from per-case rows by the validator.
    """
    coverage_limited_ids = set(COVERAGE_LIMITED_CASE_IDS)
    shell_ids = set(EIGHT_K_SHELL_CASE_IDS)
    per_case: dict[str, dict[str, Any]] = {}
    for case in cases:
        case_id = case["case_id"]
        coverage_limited = case_id in coverage_limited_ids
        state = (
            "TITLE_ONLY" if case_id == "v1f-012"
            else "METADATA_ONLY" if coverage_limited else "FULL_TEXT"
        )
        labels = case.get("acceptable_cause_labels") or ()
        direction = (
            labels[0]["direction"]
            if labels else _STAGE1_MOVE_DIRECTION.get(case_id, "unknown")
        )
        if case.get("expected_primary_evidence"):
            kind = "FULL_TEXT_BODY"
        elif case_id in shell_ids:
            kind = "EIGHT_K_SHELL"
        else:
            kind = "NONE"
        per_case[case_id] = {
            "news_content_state": state,
            "coverage_limited": coverage_limited,
            "challenge_family": _STAGE1_CHALLENGE_FAMILY[case_id],
            "primary_evidence_kind": kind,
            "move_direction": direction,
            "parent_case_id": case["lineage"]["source"].split(":", 1)[1],
        }
    return {
        "schema_version": "v1_1_stage1_stratification_v1",
        "strata": {
            "oracle_status": _counts(cases, lambda c: c["oracle_status"]),
            "move_direction": _counts(cases, lambda c: per_case[c["case_id"]]["move_direction"]),
            "cause_category": {
                "company_specific": sum(
                    1 for c in cases
                    if any(x["cause_type"] == "COMPANY_SPECIFIC_CATALYST" for x in c["acceptable_cause_labels"])
                ),
                "macro": sum(
                    1 for c in cases
                    if any(x["cause_type"] == "MACRO_EVENT" for x in c["acceptable_cause_labels"])
                ),
                "sector": sum(
                    1 for c in cases
                    if any(x["cause_type"] == "SECTOR_MOVE" for x in c["acceptable_cause_labels"])
                ),
            },
            "challenge_family": _counts(
                cases, lambda c: per_case[c["case_id"]]["challenge_family"]
            ),
            "primary_evidence": {
                "direct_primary": sum(1 for c in cases if c["expected_primary_evidence"]),
                "EIGHT_K_SHELL": sum(
                    1 for c in cases if per_case[c["case_id"]]["primary_evidence_kind"] == "EIGHT_K_SHELL"
                ),
                "no_material": sum(
                    1 for c in cases if per_case[c["case_id"]]["primary_evidence_kind"] == "NONE"
                ),
            },
            "coverage": {
                "full_text": len(cases) - len(coverage_limited_ids),
                "coverage_limited": len(coverage_limited_ids),
            },
        },
        "per_case": per_case,
    }

def _counts(cases: list[dict[str, Any]], key) -> dict[str, int]:
    out: dict[str, int] = {}
    for case in cases:
        value = key(case)
        out[value] = out.get(value, 0) + 1
    return out


def make_dataset_manifest(
    cases: list[dict[str, Any]],
    stratification: dict[str, Any] | None = None,
    *,
    approved: bool = True,
    dataset_id: str = "v1_1_stage1_fixture",
) -> dict[str, Any]:
    """Synthetic dataset manifest; authoritative approval is Q-011 gated."""
    from catalyst_eval.v1_1.loader import (
        case_list_sha256,
        dataset_content_sha256,
    )

    from catalyst_eval.v1_1.case import GoldenCase

    parsed = [GoldenCase.model_validate(row) for row in cases]
    ordered_ids = [case.case_id for case in parsed]
    manifest: dict[str, Any] = {
        "schema_version": "v1_1_stage1_dataset_manifest_v1",
        "dataset_id": dataset_id,
        "dataset_version": "1.1.0",
        "ordered_case_ids": ordered_ids,
        "case_list_sha256": case_list_sha256(ordered_ids),
        "dataset_content_sha256": dataset_content_sha256(parsed),
        "case_count": len(parsed),
        "adjudication_state": "resolved",
        "reviewer_ids": ["fixture-reviewer-1"],
        "second_pass_case_ids": ["v1f-003", "v1f-011"],
        "a3_eligible_ids": ["v1f-008"],
        "a4_readiness_eligible_ids": ["v1f-004", "v1f-008"],
    }
    if approved:
        manifest["approval_authority"] = "fixture-approval-authority"
        manifest["approved_at"] = "2026-08-19T00:00:00Z"
    if stratification is not None:
        manifest["stratification"] = stratification
    return manifest


__all__ = [
    "ALLOWED_LEGACY_PARENTS",
    "CHALLENGE_FAMILY_VALUES",
    "COVERAGE_LIMITED_CASE_IDS",
    "EIGHT_K_SHELL_CASE_IDS",
    "MOVE_DIRECTION_VALUES",
    "PRIMARY_EVIDENCE_KIND_VALUES",
    "STAGE1_FIXTURE_CASE_IDS",
    "make_case",
    "make_dataset_manifest",
    "make_stage1_cases",
    "make_stratification",
    "utc",
]


def build_fixture_eval_manifest() -> "EvalManifest":
    """Synthetic pre-execution EvalManifest for runner/CLI fixture tests."""
    from catalyst_eval.v1_1.case import GoldenCase
    from catalyst_eval.v1_1.manifest import (
        AgentPolicyIdentity,
        CodeProviderIdentity,
        DataRuntimeIdentityReference,
        EvalManifest,
        MetricContract,
        RetrievalPolicyIdentity,
    )
    from catalyst_eval.v1_1.manifest_builder import (
        Stage1DatasetInput,
        build_eval_manifest,
    )

    rows = make_stage1_cases()
    cases = tuple(GoldenCase.model_validate(row) for row in rows)
    return build_eval_manifest(
        dataset=Stage1DatasetInput(
            dataset_id="v1_1_stage1_fixture",
            dataset_version="1.1.0",
            cases=cases,
        ),
        stage="stage1",
        split="dev",
        code_identity=CodeProviderIdentity(
            code_git_sha="3037ff8", harness_revision="h:v1", random_seed=7
        ),
        data_runtime_identity=DataRuntimeIdentityReference(
            data_runtime_identity_ref="runtime-id:7a004",
            data_runtime_identity_hash="d" * 64,
        ),
        agent_policy=AgentPolicyIdentity(
            observation_policy_version="move_profile_v1",
            context_pack_policy_version="evidence_context_pack_v1",
            analyst_policy_version="bounded_competition_v1",
            writer_policy_version="writer_v1",
            a1_policy_version="a1:v1", a2_policy_version="a2:v1",
            a3_policy_version="a3:v1", a4_policy_version="a4:v1",
            a5_policy_version="a5:v1",
        ),
        retrieval_policy=RetrievalPolicyIdentity(
            arm_names=("fts5", "dense", "hybrid", "reranked"),
            arm_order=("fts5", "dense", "hybrid", "reranked"),
            top_k=8, candidate_pool_id="pool:stage1",
            dedup_policy_version="dedup:v1", independence_policy_version="ind:v1",
            reranker_policy_version="rr:v1",
        ),
        metric_spec=MetricContract(metric_spec_version="ms:v1", definitions=()),
        eligible_experiments=(),
    )
