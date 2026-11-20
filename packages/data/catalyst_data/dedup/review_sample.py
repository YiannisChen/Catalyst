"""Offline, reproducible sampling and reporting for human dedup review.

The caller supplies a frozen JSONL candidate pool and a JSON manifest next to
it. Candidate rows have this shape::

    {"stratum": "link:canonical_url",
     "record_a": {"source": "...", "document_id": "..."},
     "record_b": {"source": "...", "document_id": "..."},
     "rule_hits": [{"relation_type": "same_article",
                     "evidence_kind": "canonical_url"}],
     "corrected_times": {"record_a": "...", "record_b": "..."},
     "evidence": {"record_a": {"text": "...", "source_ref": "..."},
                  "record_b": {"text": "...", "source_ref": "..."}}}

The manifest pins the candidate bytes and this module's source bytes with
SHA-256, and declares a seed plus a quota for every required stratum. Sampling
uses a SHA-256 ordering instead of runtime-specific PRNG behavior. This module
never accesses a network, database, or evaluation dataset and never assigns
human labels.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


class ReviewSampleError(ValueError):
    """Invalid, unpinned, or insufficient review-sampling input."""


RULE_RELATIONS = {
    "provider_record_id": "same_article",
    "canonical_url": "same_article",
    "confirmed_release_id": "same_release",
    "full_body_fingerprint": "same_release",
}
REQUIRED_STRATA = tuple(
    [f"link:{rule}" for rule in RULE_RELATIONS]
    + ["title_similar_unlinked", "independent_control", "cross_source_release"]
)
# Pairs selected by source kind alone (a wire copy against an 8-K exhibit or IR
# page); rule hits are recorded as found, neither required nor forbidden.
UNCONSTRAINED_STRATA = frozenset({"cross_source_release"})
MIN_TOTAL_PAIRS = 50
LABEL_ORDER = ("same_article", "same_release", "independent", "uncertain")
LABELS = frozenset(LABEL_ORDER)
_SHA256_LENGTH = 64


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_bytes(path: Path, description: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ReviewSampleError(f"cannot read {description}: {path}") from exc


def _require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != _SHA256_LENGTH or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise ReviewSampleError(f"{field} must be a lowercase SHA-256 hex digest")
    return value


def _load_manifest(path: Path, expected_manifest_sha256: str | None) -> tuple[dict, str, bytes]:
    if expected_manifest_sha256 is None:
        raise ReviewSampleError("expected manifest SHA-256 is required")
    expected_manifest_sha256 = _require_sha256(
        expected_manifest_sha256, "expected manifest SHA-256"
    )
    raw = _read_bytes(path, "manifest")
    manifest_sha256 = _sha256(raw)
    if manifest_sha256 != expected_manifest_sha256:
        raise ReviewSampleError("manifest SHA-256 does not match the pinned value")
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewSampleError("manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ReviewSampleError("unsupported review manifest schema")
    return manifest, manifest_sha256, raw


def _candidate_path(manifest_path: Path, manifest: dict) -> Path:
    relative = manifest.get("candidates_file")
    if not isinstance(relative, str) or not relative.strip():
        raise ReviewSampleError("manifest candidates_file must be a relative path")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ReviewSampleError("manifest candidates_file must stay within its directory")
    root = manifest_path.resolve().parent
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ReviewSampleError("manifest candidates_file must stay within its directory")
    return resolved


def _validate_manifest(manifest: dict, candidate_bytes: bytes) -> tuple[str, dict[str, int], frozenset[str]]:
    expected_candidates = _require_sha256(
        manifest.get("candidates_sha256"), "candidates_sha256"
    )
    if _sha256(candidate_bytes) != expected_candidates:
        raise ReviewSampleError("candidate JSONL SHA-256 does not match the manifest")

    expected_code = _require_sha256(manifest.get("code_sha256"), "code_sha256")
    actual_code = _sha256(Path(__file__).read_bytes())
    if actual_code != expected_code:
        raise ReviewSampleError("review sampler code SHA-256 does not match the manifest")

    seed = manifest.get("seed")
    if not isinstance(seed, (str, int)) or isinstance(seed, bool) or not str(seed):
        raise ReviewSampleError("manifest seed must be a nonempty string or integer")
    raw_quotas = manifest.get("quotas")
    if not isinstance(raw_quotas, dict) or set(raw_quotas) != set(REQUIRED_STRATA):
        raise ReviewSampleError("manifest quotas must explicitly declare every required stratum")
    quotas: dict[str, int] = {}
    for stratum in REQUIRED_STRATA:
        quota = raw_quotas[stratum]
        if isinstance(quota, bool) or not isinstance(quota, int) or quota < 1:
            raise ReviewSampleError(f"quota for {stratum} must be a positive integer")
        quotas[stratum] = quota
    if sum(quotas.values()) < MIN_TOTAL_PAIRS:
        raise ReviewSampleError(f"quota total must be at least {MIN_TOTAL_PAIRS} pairs")
    take_all = manifest.get("take_all_if_short", [])
    if (not isinstance(take_all, list) or any(not isinstance(item, str) for item in take_all)
            or len(set(take_all)) != len(take_all) or not set(take_all) <= set(REQUIRED_STRATA)):
        raise ReviewSampleError("take_all_if_short must list distinct declared strata")
    return actual_code, quotas, frozenset(take_all)


def _canonical_identity(value: Any, field: str) -> bytes:
    if not isinstance(value, dict) or not value:
        raise ReviewSampleError(f"{field} must be a nonempty identity object")
    try:
        return _json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise ReviewSampleError(f"{field} must contain JSON values") from exc


def _parse_candidate(line: str, line_number: int) -> dict:
    try:
        candidate = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ReviewSampleError(f"candidate line {line_number} is invalid JSON") from exc
    if not isinstance(candidate, dict):
        raise ReviewSampleError(f"candidate line {line_number} must be a JSON object")

    stratum = candidate.get("stratum")
    if stratum not in REQUIRED_STRATA:
        raise ReviewSampleError(f"candidate line {line_number} has an unknown stratum")
    identity_a = candidate.get("record_a")
    identity_b = candidate.get("record_b")
    encoded_a = _canonical_identity(identity_a, f"line {line_number} record_a")
    encoded_b = _canonical_identity(identity_b, f"line {line_number} record_b")
    if encoded_a == encoded_b:
        raise ReviewSampleError(f"candidate line {line_number} repeats one record identity")
    reverse_records = encoded_b < encoded_a
    if reverse_records:
        encoded_a, encoded_b = encoded_b, encoded_a
        identity_a, identity_b = identity_b, identity_a

    raw_hits = candidate.get("rule_hits")
    if not isinstance(raw_hits, list):
        raise ReviewSampleError(f"candidate line {line_number} rule_hits must be a list")
    hits: list[dict[str, str]] = []
    hit_keys: set[tuple[str, str]] = set()
    for hit in raw_hits:
        if not isinstance(hit, dict):
            raise ReviewSampleError(f"candidate line {line_number} has an invalid rule hit")
        relation = hit.get("relation_type")
        rule = hit.get("evidence_kind")
        if not isinstance(rule, str) or RULE_RELATIONS.get(rule) != relation:
            raise ReviewSampleError(f"candidate line {line_number} has an unknown rule hit")
        key = (relation, rule)
        if key in hit_keys:
            raise ReviewSampleError(f"candidate line {line_number} repeats a rule hit")
        hit_keys.add(key)
        hits.append({"relation_type": relation, "evidence_kind": rule})
    hits.sort(key=lambda item: (item["evidence_kind"], item["relation_type"]))
    if stratum in UNCONSTRAINED_STRATA:
        pass
    elif stratum.startswith("link:"):
        expected_rule = stratum.removeprefix("link:")
        if not any(hit["evidence_kind"] == expected_rule for hit in hits):
            raise ReviewSampleError(f"candidate line {line_number} does not match its link stratum")
    elif hits:
        raise ReviewSampleError(f"candidate line {line_number} control stratum must have no rule hits")

    corrected_times = candidate.get("corrected_times")
    evidence = candidate.get("evidence")
    if not isinstance(corrected_times, dict) or not isinstance(evidence, dict):
        raise ReviewSampleError(
            f"candidate line {line_number} requires corrected_times and evidence objects"
        )
    if not all(
        key in corrected_times and corrected_times[key] not in (None, "")
        for key in ("record_a", "record_b")
    ):
        raise ReviewSampleError(f"candidate line {line_number} lacks corrected times for both records")
    if not all(key in evidence and isinstance(evidence[key], dict) for key in ("record_a", "record_b")):
        raise ReviewSampleError(f"candidate line {line_number} lacks review evidence for both records")
    corrected_times = dict(corrected_times)
    evidence = dict(evidence)
    if reverse_records:
        corrected_times["record_a"], corrected_times["record_b"] = (
            corrected_times["record_b"], corrected_times["record_a"]
        )
        evidence["record_a"], evidence["record_b"] = (
            evidence["record_b"], evidence["record_a"]
        )
    for record_key in ("record_a", "record_b"):
        details = evidence[record_key]
        has_text = any(
            isinstance(details.get(field), str) and details[field].strip()
            for field in ("text", "text_excerpt")
        )
        has_source = isinstance(details.get("source_ref"), str) and bool(
            details["source_ref"].strip()
        )
        if not has_text or not has_source:
            raise ReviewSampleError(
                f"candidate line {line_number} needs text and source_ref for {record_key}"
            )

    pair_material = json.dumps(
        [encoded_a.decode("utf-8"), encoded_b.decode("utf-8")],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    pair_id = _sha256(pair_material)
    return {
        "pair_id": pair_id,
        "stratum": stratum,
        "record_a": identity_a,
        "record_b": identity_b,
        "rule_hits": hits,
        "corrected_times": corrected_times,
        "evidence": evidence,
    }


def _load_candidates(candidate_bytes: bytes) -> dict[str, list[dict]]:
    try:
        source = candidate_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewSampleError("candidate JSONL must be UTF-8") from exc
    by_stratum = {stratum: [] for stratum in REQUIRED_STRATA}
    seen_pairs: dict[str, str] = {}
    for line_number, line in enumerate(source.splitlines(), start=1):
        if not line.strip():
            continue
        pair = _parse_candidate(line, line_number)
        # One pair is one review item: listing it in two strata would count its label twice.
        if pair["pair_id"] in seen_pairs:
            raise ReviewSampleError(
                f"candidate JSONL has a duplicate pair identity at line {line_number} "
                f"(strata {seen_pairs[pair['pair_id']]} and {pair['stratum']})"
            )
        seen_pairs[pair["pair_id"]] = pair["stratum"]
        by_stratum[pair["stratum"]].append(pair)
    return by_stratum


def _rank(seed: str, stratum: str, pair_id: str) -> str:
    return _sha256(f"{seed}\0{stratum}\0{pair_id}".encode("utf-8"))


def build_review_sample(
    manifest_path: str | os.PathLike[str],
    *,
    expected_manifest_sha256: str | None,
) -> dict:
    """Validate pinned inputs and return a deterministic, unlabeled review packet.

    The supplied manifest SHA is mandatory and must be recorded by the caller
    when freezing the manifest. Its candidate pool and this source file are
    independently pinned by SHA-256 values inside the manifest.
    """
    path = Path(manifest_path)
    manifest, manifest_sha256, _ = _load_manifest(path, expected_manifest_sha256)
    candidates_path = _candidate_path(path, manifest)
    candidate_bytes = _read_bytes(candidates_path, "candidate JSONL")
    code_sha256, quotas, take_all = _validate_manifest(manifest, candidate_bytes)
    by_stratum = _load_candidates(candidate_bytes)

    # A take-all-if-short stratum does not fail when short; it still yields min(quota, found) pairs.
    insufficient = {
        stratum: {"need": quotas[stratum], "found": len(by_stratum[stratum])}
        for stratum in REQUIRED_STRATA
        if len(by_stratum[stratum]) < quotas[stratum] and stratum not in take_all
    }
    if insufficient:
        details = ", ".join(
            f"{stratum}: need {counts['need']}, found {counts['found']}"
            for stratum, counts in insufficient.items()
        )
        raise ReviewSampleError(f"insufficient candidate pairs by stratum: {details}")

    sampled = {stratum: min(quotas[stratum], len(by_stratum[stratum])) for stratum in REQUIRED_STRATA}
    if sum(sampled.values()) < MIN_TOTAL_PAIRS:
        raise ReviewSampleError(
            f"fewer than {MIN_TOTAL_PAIRS} pairs would be sampled: {sum(sampled.values())} {sampled}"
        )
    seed = str(manifest["seed"])
    selected = []
    for stratum in REQUIRED_STRATA:
        ranked = sorted(
            by_stratum[stratum],
            key=lambda pair: (_rank(seed, stratum, pair["pair_id"]), pair["pair_id"]),
        )
        for pair in ranked[:sampled[stratum]]:
            selected.append({**pair, "label": "", "reason": ""})
    selected.sort(key=lambda pair: (pair["stratum"], pair["pair_id"]))
    return {
        "metadata": {
            "schema_version": 1,
            "manifest_sha256": manifest_sha256,
            "candidates_sha256": manifest["candidates_sha256"],
            "code_sha256": code_sha256,
            "seed": seed,
            "quotas": quotas,
            "take_all_if_short": sorted(take_all),
            "sampled_by_stratum": sampled,
            "pair_count": len(selected),
            "labels_filled_by_tool": False,
        },
        "pairs": selected,
    }


def write_review_sample(
    manifest_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    *,
    expected_manifest_sha256: str,
) -> dict:
    """Create a new review packet without risking an existing annotation file."""
    packet = build_review_sample(
        manifest_path, expected_manifest_sha256=expected_manifest_sha256
    )
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        with output.open("x", encoding="utf-8") as temporary:
            temporary.write(payload)
    except FileExistsError as exc:
        raise ReviewSampleError(f"review packet already exists: {output}") from exc
    except OSError as exc:
        raise ReviewSampleError(f"cannot write review packet: {output}") from exc
    return packet


def summarize_review_sample(
    packet_or_path: dict | str | os.PathLike[str],
    *,
    manifest_path: str | os.PathLike[str],
    expected_manifest_sha256: str,
) -> dict:
    """Verify a completed packet against its frozen sample, then summarize labels."""
    if isinstance(packet_or_path, dict):
        packet = packet_or_path
    else:
        path = Path(packet_or_path)
        raw = _read_bytes(path, "review packet")
        try:
            packet = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewSampleError("review packet is not valid UTF-8 JSON") from exc
    if (
        not isinstance(packet, dict)
        or not isinstance(packet.get("metadata"), dict)
        or packet["metadata"].get("schema_version") != 1
    ):
        raise ReviewSampleError("unsupported review packet schema")
    frozen = build_review_sample(
        manifest_path, expected_manifest_sha256=expected_manifest_sha256
    )
    if packet["metadata"] != frozen["metadata"]:
        raise ReviewSampleError("review packet metadata differs from the frozen sample")
    pairs = packet.get("pairs")
    if not isinstance(pairs, list):
        raise ReviewSampleError("review packet pairs must be a list")
    frozen_by_id = {pair["pair_id"]: pair for pair in frozen["pairs"]}
    submitted_by_id = {}
    for pair in pairs:
        if not isinstance(pair, dict):
            raise ReviewSampleError("review packet contains a non-object pair")
        pair_id = pair.get("pair_id")
        if not isinstance(pair_id, str) or pair_id not in frozen_by_id:
            raise ReviewSampleError("review packet pair_id differs from the frozen sample")
        if pair_id in submitted_by_id:
            raise ReviewSampleError("review packet contains a duplicate pair_id")
        non_annotation_fields = {
            key: value for key, value in pair.items() if key not in {"label", "reason"}
        }
        frozen_fields = {
            key: value
            for key, value in frozen_by_id[pair_id].items()
            if key not in {"label", "reason"}
        }
        if non_annotation_fields != frozen_fields:
            raise ReviewSampleError("review packet differs from the frozen sample")
        submitted_by_id[pair_id] = pair
    if set(submitted_by_id) != set(frozen_by_id):
        raise ReviewSampleError("review packet pair set differs from the frozen sample")

    total = len(pairs)
    counts = {label: 0 for label in LABEL_ORDER}
    for pair in pairs:
        label = pair.get("label", "")
        if not isinstance(label, str) or (label not in LABELS and label != ""):
            raise ReviewSampleError(f"unknown human label: {label}")
        if label:
            reason = pair.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ReviewSampleError("every assigned label requires a human reason")
            counts[label] += 1
        pair_hits = pair.get("rule_hits", [])
        if not isinstance(pair_hits, list):
            raise ReviewSampleError("review packet pair rule_hits must be a list")
        for hit in pair_hits:
            if not isinstance(hit, dict) or RULE_RELATIONS.get(hit.get("evidence_kind")) != hit.get("relation_type"):
                raise ReviewSampleError("review packet contains an invalid deterministic rule hit")

    adjudicable_n = counts["same_article"] + counts["same_release"] + counts["independent"]
    uncertain_n = counts["uncertain"]
    unlabeled_n = total - adjudicable_n - uncertain_n
    rules = {}
    for rule, relation in RULE_RELATIONS.items():
        tp = fp = fn = 0
        for pair in pairs:
            label = pair.get("label", "")
            if label not in {"same_article", "same_release", "independent"}:
                continue
            predicted = any(
                hit.get("evidence_kind") == rule
                for hit in pair["rule_hits"]
            )
            if predicted and label == relation:
                tp += 1
            elif predicted:
                fp += 1
            elif label == relation:
                fn += 1
        rules[rule] = {
            "relation_type": relation,
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "adjudicable_n": adjudicable_n,
            "excluded_uncertain_n": uncertain_n,
            "excluded_unlabeled_n": unlabeled_n,
        }

    strata = {}
    for stratum in REQUIRED_STRATA:
        subset = [pair for pair in pairs if pair.get("stratum") == stratum]
        strata[stratum] = {
            "sampled_n": len(subset),
            "adjudicable_n": sum(
                pair.get("label") in {"same_article", "same_release", "independent"}
                for pair in subset
            ),
            "uncertain_n": sum(pair.get("label") == "uncertain" for pair in subset),
            "unlabeled_n": sum(pair.get("label", "") == "" for pair in subset),
        }
    metadata = packet.get("metadata", {})
    return {
        "metadata": {
            "manifest_sha256": metadata.get("manifest_sha256"),
            "candidates_sha256": metadata.get("candidates_sha256"),
            "code_sha256": metadata.get("code_sha256"),
            "seed": metadata.get("seed"),
            "quotas": metadata.get("quotas"),
            "take_all_if_short": metadata.get("take_all_if_short"),
            "sampled_by_stratum": metadata.get("sampled_by_stratum"),
        },
        "overall": {
            "sampled_n": total,
            "adjudicable_n": adjudicable_n,
            "uncertain_n": uncertain_n,
            "unlabeled_n": unlabeled_n,
            "label_counts": counts,
        },
        "strata": strata,
        "rules": rules,
        "claim_scope": "sampled candidate strata only; not overall internet recall",
    }


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sample = commands.add_parser("sample", help="write an unlabeled review packet")
    sample.add_argument("manifest", type=Path)
    sample.add_argument("--manifest-sha256", required=True)
    sample.add_argument("--output", required=True, type=Path)
    summary = commands.add_parser("summarize", help="summarize completed human labels")
    summary.add_argument("packet", type=Path)
    summary.add_argument("--manifest", required=True, type=Path)
    summary.add_argument("--manifest-sha256", required=True)
    summary.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "sample":
            write_review_sample(
                args.manifest,
                args.output,
                expected_manifest_sha256=args.manifest_sha256,
            )
            return 0
        report = summarize_review_sample(
            args.packet,
            manifest_path=args.manifest,
            expected_manifest_sha256=args.manifest_sha256,
        )
        serialized = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if args.output:
            try:
                with args.output.open("x", encoding="utf-8") as output_file:
                    output_file.write(serialized)
            except FileExistsError as exc:
                raise ReviewSampleError(
                    f"summary output already exists: {args.output}"
                ) from exc
            except OSError as exc:
                raise ReviewSampleError(
                    f"cannot write summary output: {args.output}"
                ) from exc
        else:
            sys.stdout.write(serialized)
        return 0
    except ReviewSampleError as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(_main())
