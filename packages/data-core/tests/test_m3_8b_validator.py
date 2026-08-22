"""M3-8B operator-input validator tests (Corrective Batch C C8).

The validator lives in the repository (``catalyst_data.sec.m3_8b_validator``);
these tests build operator records under ``tmp_path`` only and never write
``data/baseline/benchmark_accessions_v1.json`` or
``data/baseline/q005_sec_time_approval_v1.json``.
"""
from __future__ import annotations

import json as _json
from pathlib import Path

import pytest

from catalyst_data.canonical.ids import sha256_identity
from catalyst_data.sec.m3_8b_validator import validate_m3_8b_operator_inputs

ACC = "0000320193-26-000001"
ACC2 = "0000320193-26-000002"
ACC3 = "0000320193-26-000003"
GIT_REV = "b6785abefd80abcb22b993e300eb22246b61f565"
AUTHORITY = "Frozen §10 + Final TSD §5.3 interpretation authority"
FROZEN = "2026-08-22T00:00:00Z"


def _case_hash(accessions, selection_authority=AUTHORITY) -> str:
    return sha256_identity(
        {
            "schema_version": "benchmark_accessions_v1",
            "selection_authority": selection_authority,
            "ordered_unique_accession_ids": list(accessions),
        }
    )


def _benchmark(
    accessions,
    *,
    schema_version="benchmark_accessions_v1",
    selection_authority=AUTHORITY,
    frozen_at=FROZEN,
    git_revision=GIT_REV,
    denominator=None,
    case_list_sha256=None,
    excluded_accessions=None,
    selection_exclusions=None,
    **overrides,
) -> dict:
    data = {
        "schema_version": schema_version,
        "selection_authority": selection_authority,
        "frozen_at": frozen_at,
        "git_revision": git_revision,
        "ordered_unique_accession_ids": list(accessions),
        "excluded_accessions": [] if excluded_accessions is None else excluded_accessions,
        "selection_exclusions": (
            [] if selection_exclusions is None else selection_exclusions
        ),
        "case_list_sha256": (
            case_list_sha256
            if case_list_sha256 is not None
            else _case_hash(accessions, selection_authority)
        ),
        "denominator": len(accessions) if denominator is None else denominator,
    }
    data.update(overrides)
    return data


def _q005(
    accessions,
    *,
    schema_version="q005_sec_time_approval_v1",
    benchmark_case_list_sha256=None,
    selection_authority=AUTHORITY,
    decision="fail_closed_only",
    raw_payload_inventory=None,
    frozen_at=FROZEN,
    git_revision=GIT_REV,
    **overrides,
) -> dict:
    data = {
        "schema_version": schema_version,
        "benchmark_case_list_sha256": (
            benchmark_case_list_sha256
            if benchmark_case_list_sha256 is not None
            else _case_hash(accessions, selection_authority)
        ),
        "selection_authority": selection_authority,
        "decision": decision,
        "raw_payload_inventory": (
            raw_payload_inventory
            if raw_payload_inventory is not None
            else [
                {"accession": a, "acceptance_datetime_retained": True}
                for a in accessions
            ]
        ),
        "frozen_at": frozen_at,
        "git_revision": git_revision,
    }
    data.update(overrides)
    return data


def _write(tmp_path, *, benchmark, q005, prefix="records") -> tuple[Path, Path]:
    bpath = tmp_path / f"{prefix}-benchmark.json"
    qpath = tmp_path / f"{prefix}-q005.json"
    bpath.write_text(_json.dumps(benchmark, sort_keys=True))
    qpath.write_text(_json.dumps(q005, sort_keys=True))
    return bpath, qpath


def _valid(tmp_path, accessions=(ACC, ACC2)):
    return _write(
        tmp_path,
        benchmark=_benchmark(accessions),
        q005=_q005(accessions),
    )


def test_exact_schema_versions(tmp_path):
    b, q = _valid(tmp_path)
    validate_m3_8b_operator_inputs(b, q, expected_git_revision=GIT_REV)

    b_bad, _ = _write(
        tmp_path,
        benchmark=_benchmark([ACC], schema_version="benchmark_accessions_v2"),
        q005=_q005([ACC]),
        prefix="bad-bench-schema",
    )
    with pytest.raises(ValueError, match="schema_version"):
        validate_m3_8b_operator_inputs(b_bad, q, expected_git_revision=GIT_REV)

    _, q_bad = _write(
        tmp_path,
        benchmark=_benchmark([ACC]),
        q005=_q005([ACC], schema_version="q005_sec_time_approval_v2"),
        prefix="bad-q005-schema",
    )
    with pytest.raises(ValueError, match="schema_version"):
        validate_m3_8b_operator_inputs(b, q_bad, expected_git_revision=GIT_REV)


@pytest.mark.parametrize(
    "which",
    ["benchmark", "q005"],
)
def test_git_revision_must_match_expected(tmp_path, which):
    b, q = _valid(tmp_path)
    if which == "benchmark":
        b, _ = _write(
            tmp_path,
            benchmark=_benchmark([ACC], git_revision="deadbeef"),
            q005=_q005([ACC]),
            prefix="rev-bench",
        )
    else:
        _, q = _write(
            tmp_path,
            benchmark=_benchmark([ACC]),
            q005=_q005([ACC], git_revision="deadbeef"),
            prefix="rev-q005",
        )
    with pytest.raises(ValueError, match="git_revision"):
        validate_m3_8b_operator_inputs(b, q, expected_git_revision=GIT_REV)


@pytest.mark.parametrize(
    "accessions",
    [[], [ACC, ACC]],
)
def test_non_empty_unique_accession_list(tmp_path, accessions):
    b, q = _write(
        tmp_path,
        benchmark=_benchmark(accessions),
        q005=_q005([ACC] if accessions else [ACC]),
        prefix="list",
    )
    with pytest.raises(ValueError):
        validate_m3_8b_operator_inputs(b, q, expected_git_revision=GIT_REV)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "  ",
        "0000320193-26-000001 ",
        "000032019326000001",
        "0000320193-26-00000X",
        "not-an-accession",
        None,
        123,
    ],
)
def test_accession_grammar(tmp_path, bad):
    b, q = _write(
        tmp_path,
        benchmark=_benchmark([ACC, bad]),
        q005=_q005([ACC]),
        prefix="grammar",
    )
    with pytest.raises(ValueError, match="accession"):
        validate_m3_8b_operator_inputs(b, q, expected_git_revision=GIT_REV)


def test_denominator_must_equal_len_a(tmp_path):
    b, q = _write(
        tmp_path,
        benchmark=_benchmark([ACC, ACC2], denominator=99),
        q005=_q005([ACC, ACC2]),
        prefix="denom",
    )
    with pytest.raises(ValueError, match="denominator"):
        validate_m3_8b_operator_inputs(b, q, expected_git_revision=GIT_REV)


@pytest.mark.parametrize(
    "overrides",
    [
        {"selection_exclusions": [{"accession": ACC2, "reason": "post-hoc"}]},
        {"excluded_accessions": [ACC2]},
    ],
)
def test_exclusions_never_members_of_a(tmp_path, overrides):
    b, q = _write(
        tmp_path,
        benchmark=_benchmark([ACC, ACC2], **overrides),
        q005=_q005([ACC, ACC2]),
        prefix="excl",
    )
    with pytest.raises(ValueError, match="exclusions"):
        validate_m3_8b_operator_inputs(b, q, expected_git_revision=GIT_REV)


def test_non_member_exclusion_passes_and_never_subtracts(tmp_path):
    b, q = _write(
        tmp_path,
        benchmark=_benchmark(
            [ACC, ACC2],
            excluded_accessions=["0000320193-26-099999"],
            selection_exclusions=[
                {"accession": "0000320193-26-099998", "reason": "selection-stage"}
            ],
        ),
        q005=_q005([ACC, ACC2]),
        prefix="excl-ok",
    )
    validate_m3_8b_operator_inputs(b, q, expected_git_revision=GIT_REV)


def test_case_list_sha256_recomputes(tmp_path):
    b, q = _write(
        tmp_path,
        benchmark=_benchmark([ACC, ACC2], case_list_sha256="f" * 64),
        q005=_q005([ACC, ACC2]),
        prefix="hash",
    )
    with pytest.raises(ValueError, match="case_list_sha256"):
        validate_m3_8b_operator_inputs(b, q, expected_git_revision=GIT_REV)


def test_case_list_sha256_reject_extra_payload_keys(tmp_path):
    # A hash computed over a payload that includes denominator must not pass.
    forged = sha256_identity(
        {
            "schema_version": "benchmark_accessions_v1",
            "selection_authority": AUTHORITY,
            "ordered_unique_accession_ids": [ACC, ACC2],
            "denominator": 2,
        }
    )
    b, q = _write(
        tmp_path,
        benchmark=_benchmark([ACC, ACC2], case_list_sha256=forged),
        q005=_q005([ACC, ACC2], benchmark_case_list_sha256=forged),
        prefix="forged",
    )
    with pytest.raises(ValueError, match="case_list_sha256"):
        validate_m3_8b_operator_inputs(b, q, expected_git_revision=GIT_REV)


@pytest.mark.parametrize(
    "decision", ["approve_everything", "", None, "approve_latest_plausible"],
)
def test_q005_decision_literal(tmp_path, decision):
    b, q = _write(
        tmp_path,
        benchmark=_benchmark([ACC]),
        q005=_q005([ACC], decision=decision),
        prefix="decision",
    )
    with pytest.raises(ValueError, match="decision"):
        validate_m3_8b_operator_inputs(b, q, expected_git_revision=GIT_REV)


@pytest.mark.parametrize(
    "inventory",
    [
        [{"accession": ACC, "acceptance_datetime_retained": True}],  # missing ACC2
        [
            {"accession": ACC, "acceptance_datetime_retained": True},
            {"accession": ACC2, "acceptance_datetime_retained": True},
            {"accession": ACC2, "acceptance_datetime_retained": True},
        ],  # duplicate
        [
            {"accession": ACC, "acceptance_datetime_retained": True},
            {"accession": ACC2, "acceptance_datetime_retained": True},
            {"accession": ACC3, "acceptance_datetime_retained": True},
        ],  # extra
        ["not-an-object"],
    ],
)
def test_q005_inventory_exactly_covers_a(tmp_path, inventory):
    b, q = _write(
        tmp_path,
        benchmark=_benchmark([ACC, ACC2]),
        q005=_q005([ACC, ACC2], raw_payload_inventory=inventory),
        prefix="inventory",
    )
    with pytest.raises(ValueError, match="raw_payload_inventory"):
        validate_m3_8b_operator_inputs(b, q, expected_git_revision=GIT_REV)


def test_q005_hash_matches_benchmark_hash(tmp_path):
    b, q = _write(
        tmp_path,
        benchmark=_benchmark([ACC, ACC2]),
        q005=_q005([ACC, ACC2], benchmark_case_list_sha256="e" * 64),
        prefix="match",
    )
    with pytest.raises(ValueError, match="benchmark_case_list_sha256"):
        validate_m3_8b_operator_inputs(b, q, expected_git_revision=GIT_REV)


@pytest.mark.parametrize(
    "which", ["benchmark", "q005"],
)
def test_selection_authority_non_empty(tmp_path, which):
    # Use consistent record pairs whose hashes agree so the authority check is
    # the violation that fires.
    if which == "benchmark":
        b, q = _write(
            tmp_path,
            benchmark=_benchmark([ACC], selection_authority="  "),
            q005=_q005(
                [ACC],
                benchmark_case_list_sha256=_case_hash([ACC], "  "),
            ),
            prefix="authority-bench",
        )
    else:
        b, q = _write(
            tmp_path,
            benchmark=_benchmark([ACC]),
            q005=_q005(
                [ACC],
                selection_authority="",
                benchmark_case_list_sha256=_case_hash([ACC]),
            ),
            prefix="authority-q005",
        )
    with pytest.raises(ValueError, match="selection_authority"):
        validate_m3_8b_operator_inputs(b, q, expected_git_revision=GIT_REV)


@pytest.mark.parametrize(
    "frozen_at",
    [
        "2026-08-22T00:00:00",  # naive
        "2026-08-22T08:00:00+08:00",  # aware but not UTC
        "not-a-time",
        "",
        None,
    ],
)
def test_frozen_at_timezone_aware_utc_iso(tmp_path, frozen_at):
    b, q = _write(
        tmp_path,
        benchmark=_benchmark([ACC], frozen_at=frozen_at),
        q005=_q005([ACC], frozen_at=frozen_at),
        prefix="frozen",
    )
    with pytest.raises(ValueError, match="frozen_at"):
        validate_m3_8b_operator_inputs(b, q, expected_git_revision=GIT_REV)


def test_error_strings_are_secret_and_path_safe(tmp_path):
    home = str(Path.home())
    # Violations must not leak the record paths or the home directory.
    bad_bench, _ = _write(
        tmp_path,
        benchmark=_benchmark([ACC], schema_version="benchmark_accessions_v2"),
        q005=_q005([ACC]),
        prefix="leak-bench",
    )
    with pytest.raises(ValueError) as exc:
        validate_m3_8b_operator_inputs(bad_bench, _, expected_git_revision=GIT_REV)
    msg = str(exc.value)
    assert str(tmp_path) not in msg
    assert home not in msg

    good_bench, bad_q = _write(
        tmp_path,
        benchmark=_benchmark([ACC]),
        q005=_q005([ACC], decision="approve_everything"),
        prefix="leak-q005",
    )
    with pytest.raises(ValueError) as exc:
        validate_m3_8b_operator_inputs(good_bench, bad_q, expected_git_revision=GIT_REV)
    msg = str(exc.value)
    assert str(tmp_path) not in msg
    assert home not in msg
