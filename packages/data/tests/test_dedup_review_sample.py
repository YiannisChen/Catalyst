"""Synthetic-only tests for reproducible offline dedup review sampling."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from catalyst_data.dedup import review_sample
from catalyst_data.dedup.review_sample import (
    REQUIRED_STRATA,
    ReviewSampleError,
    build_review_sample,
    summarize_review_sample,
    write_review_sample,
)


def _write_inputs(tmp_path: Path, *, shortage: str | None = None, counts: dict | None = None,
                  take_all: list | None = None, cross_hits: bool = False, control_hits: bool = False,
                  extra: list | None = None):
    candidates_path = tmp_path / "candidates.jsonl"
    candidates = []
    for stratum in REQUIRED_STRATA:
        count = (counts or {}).get(stratum, 9 if stratum == shortage else 10)
        rule_hits = []
        if stratum.startswith("link:"):
            rule = stratum.removeprefix("link:")
            relation = "same_release" if rule in {
                "confirmed_release_id", "full_body_fingerprint"
            } else "same_article"
            rule_hits = [{"relation_type": relation, "evidence_kind": rule}]
        elif (stratum == "cross_source_release" and cross_hits) or (
                stratum == "independent_control" and control_hits):
            rule_hits = [{"relation_type": "same_release", "evidence_kind": "full_body_fingerprint"}]
        for index in range(count):
            candidates.append({
                "stratum": stratum,
                "record_a": {"source": "synthetic-a", "document_id": f"a-{stratum}-{index}"},
                "record_b": {"source": "synthetic-b", "document_id": f"b-{stratum}-{index}"},
                "rule_hits": rule_hits,
                "corrected_times": {"record_a": "2026-01-01T00:00:00Z", "record_b": "2026-01-01T00:00:01Z"},
                "evidence": {
                    "record_a": {"text": f"Synthetic text A {index}", "source_ref": "fixture://a"},
                    "record_b": {"text": f"Synthetic text B {index}", "source_ref": "fixture://b"},
                },
            })
    candidates.extend(extra or [])
    candidates_path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in candidates))
    quotas = {stratum: 10 for stratum in REQUIRED_STRATA}
    manifest = {
        "schema_version": 1,
        "candidates_file": candidates_path.name,
        "candidates_sha256": hashlib.sha256(candidates_path.read_bytes()).hexdigest(),
        "code_sha256": hashlib.sha256(Path(review_sample.__file__).read_bytes()).hexdigest(),
        "seed": "review-round-1",
        "quotas": quotas,
    }
    if take_all is not None:
        manifest["take_all_if_short"] = take_all
    manifest_path = tmp_path / "review_manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    return manifest_path, manifest_sha256


def test_sampling_is_reproducible_and_leaves_labels_blank(tmp_path):
    manifest_path, manifest_sha256 = _write_inputs(tmp_path)

    first = build_review_sample(manifest_path, expected_manifest_sha256=manifest_sha256)
    second = build_review_sample(manifest_path, expected_manifest_sha256=manifest_sha256)

    assert first == second
    assert len(first["pairs"]) == 70
    assert {row["stratum"] for row in first["pairs"]} == set(REQUIRED_STRATA)
    assert all(row["label"] == "" and row["reason"] == "" for row in first["pairs"])
    assert all("Synthetic text" in row["evidence"]["record_a"]["text"] for row in first["pairs"])
    assert first["metadata"]["seed"] == "review-round-1"
    assert len(first["metadata"]["code_sha256"]) == 64


def test_reversed_identities_keep_times_and_evidence_attached_to_the_record(tmp_path):
    manifest_path, _ = _write_inputs(tmp_path)
    candidates_path = tmp_path / "candidates.jsonl"
    rows = [json.loads(line) for line in candidates_path.read_text().splitlines()]
    candidate = rows[0]
    candidate["record_a"], candidate["record_b"] = candidate["record_b"], candidate["record_a"]
    candidate["corrected_times"]["record_a"], candidate["corrected_times"]["record_b"] = (
        candidate["corrected_times"]["record_b"], candidate["corrected_times"]["record_a"]
    )
    candidate["evidence"]["record_a"], candidate["evidence"]["record_b"] = (
        candidate["evidence"]["record_b"], candidate["evidence"]["record_a"]
    )
    encode = lambda identity: json.dumps(identity, sort_keys=True, separators=(",", ":"))
    assert encode(candidate["record_b"]) < encode(candidate["record_a"])
    candidates_path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    manifest = json.loads(manifest_path.read_text())
    manifest["candidates_sha256"] = hashlib.sha256(candidates_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    packet = build_review_sample(manifest_path, expected_manifest_sha256=manifest_sha256)
    target_document = "a-link:provider_record_id-0"
    sampled = next(
        row for row in packet["pairs"]
        if row["stratum"] == REQUIRED_STRATA[0]
        and target_document in {
            row["record_a"]["document_id"], row["record_b"]["document_id"]
        }
    )

    assert sampled["record_a"]["source"] == "synthetic-a"
    assert sampled["corrected_times"]["record_a"] == "2026-01-01T00:00:00Z"
    assert sampled["evidence"]["record_a"]["text"].startswith("Synthetic text A")


def test_manifest_and_candidate_bytes_are_pinned(tmp_path):
    manifest_path, manifest_sha256 = _write_inputs(tmp_path)
    manifest_path.with_name("candidates.jsonl").write_text("{}\n")

    with pytest.raises(ReviewSampleError, match="candidate.*SHA-256"):
        build_review_sample(manifest_path, expected_manifest_sha256=manifest_sha256)


def test_sample_writer_refuses_to_overwrite_existing_human_work(tmp_path):
    manifest_path, manifest_sha256 = _write_inputs(tmp_path)
    output_path = tmp_path / "review_packet.json"
    output_path.write_text("human annotations must survive")

    with pytest.raises(ReviewSampleError, match="already exists"):
        write_review_sample(
            manifest_path,
            output_path,
            expected_manifest_sha256=manifest_sha256,
        )
    assert output_path.read_text() == "human annotations must survive"


def test_manifest_sha_must_be_explicit_and_match(tmp_path):
    manifest_path, manifest_sha256 = _write_inputs(tmp_path)

    with pytest.raises(ReviewSampleError, match="manifest SHA-256"):
        build_review_sample(manifest_path, expected_manifest_sha256="0" * 64)
    with pytest.raises(ReviewSampleError, match="expected manifest SHA-256"):
        build_review_sample(manifest_path, expected_manifest_sha256=None)


def test_shortage_is_reported_without_changing_quotas(tmp_path):
    manifest_path, manifest_sha256 = _write_inputs(
        tmp_path, shortage="independent_control"
    )

    with pytest.raises(ReviewSampleError, match="independent_control.*need 10.*found 9"):
        build_review_sample(manifest_path, expected_manifest_sha256=manifest_sha256)


def test_summary_excludes_blank_and_uncertain_labels_and_reports_rule_denominators(tmp_path):
    manifest_path, manifest_sha256 = _write_inputs(tmp_path)
    packet = build_review_sample(manifest_path, expected_manifest_sha256=manifest_sha256)
    provider_rows = [
        row for row in packet["pairs"] if row["stratum"] == "link:provider_record_id"
    ]
    provider_rows[0]["label"] = "same_article"
    provider_rows[0]["reason"] = "Human-confirmed synthetic example."
    provider_rows[1]["label"] = "independent"
    provider_rows[1]["reason"] = "Human-confirmed synthetic example."
    provider_rows[2]["label"] = "uncertain"
    provider_rows[2]["reason"] = "Insufficient synthetic evidence."
    provider_rows[3]["label"] = ""
    packet_path = tmp_path / "review_packet.json"
    packet_path.write_text(json.dumps(packet))

    report = summarize_review_sample(
        packet_path,
        manifest_path=manifest_path,
        expected_manifest_sha256=manifest_sha256,
    )

    provider_metrics = report["rules"]["provider_record_id"]
    assert provider_metrics == {
        "relation_type": "same_article",
        "tp": 1,
        "fp": 1,
        "fn": 0,
        "adjudicable_n": 2,
        "excluded_uncertain_n": 1,
        "excluded_unlabeled_n": 67,
    }
    assert report["overall"]["adjudicable_n"] == 2
    assert report["overall"]["uncertain_n"] == 1
    assert report["overall"]["unlabeled_n"] == 67
    assert report["claim_scope"] == "sampled candidate strata only; not overall internet recall"


@pytest.mark.parametrize("tamper", ["pair_id", "rule_hits", "evidence"])
def test_summary_rebuilds_and_rejects_modified_non_annotation_fields(tmp_path, tamper):
    manifest_path, manifest_sha256 = _write_inputs(tmp_path)
    packet = build_review_sample(manifest_path, expected_manifest_sha256=manifest_sha256)
    row = next(row for row in packet["pairs"] if row["rule_hits"])
    row["label"] = "same_article"
    row["reason"] = "Human-confirmed synthetic example."
    if tamper == "pair_id":
        row["pair_id"] = "f" * 64
    elif tamper == "rule_hits":
        row["rule_hits"] = []
    else:
        row["evidence"]["record_a"]["text"] = "edited evidence"
    packet_path = tmp_path / f"tampered-{tamper}.json"
    packet_path.write_text(json.dumps(packet))

    with pytest.raises(ReviewSampleError, match="differs from the frozen sample"):
        summarize_review_sample(
            packet_path,
            manifest_path=manifest_path,
            expected_manifest_sha256=manifest_sha256,
        )


def test_summary_cli_refuses_to_overwrite_existing_human_report(tmp_path, capsys):
    manifest_path, manifest_sha256 = _write_inputs(tmp_path)
    packet = build_review_sample(manifest_path, expected_manifest_sha256=manifest_sha256)
    packet_path = tmp_path / "review_packet.json"
    packet_path.write_text(json.dumps(packet))
    output_path = tmp_path / "human_report.json"
    output_path.write_text("existing human report")

    with pytest.raises(SystemExit) as exc_info:
        review_sample._main([
            "summarize",
            str(packet_path),
            "--manifest",
            str(manifest_path),
            "--manifest-sha256",
            manifest_sha256,
            "--output",
            str(output_path),
        ])

    assert exc_info.value.code == 2
    assert "already exists" in capsys.readouterr().err
    assert output_path.read_text() == "existing human report"


def test_invalid_or_duplicate_pair_identity_is_rejected(tmp_path):
    manifest_path, _ = _write_inputs(tmp_path)
    candidates_path = tmp_path / "candidates.jsonl"
    rows = [json.loads(line) for line in candidates_path.read_text().splitlines()]
    rows[1]["record_a"] = rows[0]["record_a"]
    rows[1]["record_b"] = rows[0]["record_b"]
    candidates_path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    manifest = json.loads(manifest_path.read_text())
    manifest["candidates_sha256"] = hashlib.sha256(candidates_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    with pytest.raises(ReviewSampleError, match="duplicate pair identity"):
        build_review_sample(manifest_path, expected_manifest_sha256=manifest_sha256)


def test_take_all_if_short_samples_every_pair_and_reports_the_actual_count(tmp_path):
    manifest_path, manifest_sha256 = _write_inputs(
        tmp_path, counts={"link:confirmed_release_id": 0, "cross_source_release": 3},
        take_all=["link:confirmed_release_id", "cross_source_release"], cross_hits=True,
    )
    packet = build_review_sample(manifest_path, expected_manifest_sha256=manifest_sha256)
    sampled = packet["metadata"]["sampled_by_stratum"]
    assert sampled["link:confirmed_release_id"] == 0
    assert sampled["cross_source_release"] == 3
    assert packet["metadata"]["take_all_if_short"] == ["cross_source_release", "link:confirmed_release_id"]
    assert packet["metadata"]["pair_count"] == len(packet["pairs"]) == sum(sampled.values())
    # A stratum not declared take-all still fails on shortage.
    strict = tmp_path / "strict"
    strict.mkdir()
    manifest_path, manifest_sha256 = _write_inputs(
        strict, counts={"independent_control": 9, "cross_source_release": 3},
        take_all=["cross_source_release"],
    )
    with pytest.raises(ReviewSampleError, match="independent_control"):
        build_review_sample(manifest_path, expected_manifest_sha256=manifest_sha256)


@pytest.mark.parametrize("control_count, ok", [(9, False), (10, True)])
def test_take_all_if_short_requires_at_least_fifty_sampled_pairs(tmp_path, control_count, ok):
    # Four strata of 10, two empty take-all strata, and a short take-all control: 49 or 50 pairs.
    counts = {stratum: 10 for stratum in REQUIRED_STRATA}
    counts.update({"link:confirmed_release_id": 0, "cross_source_release": 0,
                   "independent_control": control_count})
    manifest_path, manifest_sha256 = _write_inputs(
        tmp_path, counts=counts,
        take_all=["link:confirmed_release_id", "cross_source_release", "independent_control"],
    )
    if ok:
        packet = build_review_sample(manifest_path, expected_manifest_sha256=manifest_sha256)
        assert packet["metadata"]["pair_count"] == 50
    else:
        with pytest.raises(ReviewSampleError, match="fewer than 50"):
            build_review_sample(manifest_path, expected_manifest_sha256=manifest_sha256)


def test_cross_source_shortage_fails_unless_declared_take_all(tmp_path):
    manifest_path, manifest_sha256 = _write_inputs(tmp_path, counts={"cross_source_release": 3})
    with pytest.raises(ReviewSampleError, match="cross_source_release.*need 10.*found 3"):
        build_review_sample(manifest_path, expected_manifest_sha256=manifest_sha256)


def test_take_all_if_short_samples_only_the_quota_when_the_pool_is_larger(tmp_path):
    manifest_path, manifest_sha256 = _write_inputs(
        tmp_path, counts={"cross_source_release": 15}, take_all=["cross_source_release"],
    )
    packet = build_review_sample(manifest_path, expected_manifest_sha256=manifest_sha256)
    assert packet["metadata"]["sampled_by_stratum"]["cross_source_release"] == 10


def test_a_pair_listed_in_two_strata_rejects_the_pool_and_names_both(tmp_path):
    duplicate = {
        "stratum": "cross_source_release",
        "record_a": {"source": "synthetic-a", "document_id": "a-link:full_body_fingerprint-0"},
        "record_b": {"source": "synthetic-b", "document_id": "b-link:full_body_fingerprint-0"},
        "rule_hits": [{"relation_type": "same_release", "evidence_kind": "full_body_fingerprint"}],
        "corrected_times": {"record_a": "2026-01-01T00:00:00Z", "record_b": "2026-01-01T00:00:01Z"},
        "evidence": {"record_a": {"text": "A", "source_ref": "fixture://a"},
                     "record_b": {"text": "B", "source_ref": "fixture://b"}},
    }
    manifest_path, manifest_sha256 = _write_inputs(tmp_path, extra=[duplicate])
    with pytest.raises(ReviewSampleError, match="link:full_body_fingerprint and cross_source_release"):
        build_review_sample(manifest_path, expected_manifest_sha256=manifest_sha256)


@pytest.mark.parametrize("take_all", ["cross_source_release", ["unknown"], ["cross_source_release"] * 2,
                                     [{}], [["cross_source_release"]]])
def test_take_all_if_short_must_list_distinct_declared_strata(tmp_path, take_all):
    manifest_path, manifest_sha256 = _write_inputs(tmp_path, take_all=take_all)
    with pytest.raises(ReviewSampleError, match="take_all_if_short"):
        build_review_sample(manifest_path, expected_manifest_sha256=manifest_sha256)


def test_control_strata_still_reject_rule_hits_while_cross_source_accepts_them(tmp_path):
    manifest_path, manifest_sha256 = _write_inputs(tmp_path, cross_hits=True)
    packet = build_review_sample(manifest_path, expected_manifest_sha256=manifest_sha256)
    cross = [pair for pair in packet["pairs"] if pair["stratum"] == "cross_source_release"]
    assert cross and all(pair["rule_hits"] for pair in cross)
    controls = tmp_path / "controls"
    controls.mkdir()
    manifest_path, manifest_sha256 = _write_inputs(controls, cross_hits=True, control_hits=True)
    with pytest.raises(ReviewSampleError, match="control stratum must have no rule hits"):
        build_review_sample(manifest_path, expected_manifest_sha256=manifest_sha256)
