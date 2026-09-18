"""M8 contract tests: sealed general source-selection manifest."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from catalyst_data.canonical.source_selection import (
    SELECTION_POLICY_ID,
    load_source_selection_manifest,
    source_selection_id,
)


def _write_body(root: Path, name: str, text: str) -> dict[str, str]:
    body = root / name
    body.parent.mkdir(parents=True, exist_ok=True)
    body.write_text(text, encoding="utf-8")
    return {
        "body_path": name,
        "body_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def valid_manifest(root: Path, *, generated_at: str | None = None) -> dict:
    document = {
        "canonical_url": "https://www.sec.gov/example-8k",
        "source_class": "issuer_disclosure",
        "source_published_at": "2025-05-01T20:00:00Z",
        "eligible_at": "2025-05-01T20:00:00Z",
        "fetched_at": "2025-05-02T00:00:00Z",
        "provider": "sec",
        "publisher": "sec",
        "tickers": ["AAPL"],
        **_write_body(root, "bodies/doc-1.txt", "material filing body"),
    }
    payload = {
        "schema_version": "v1_1_source_selection_v1",
        "selection_policy_id": SELECTION_POLICY_ID,
        "documents": [document],
    }
    if generated_at is not None:
        payload["generated_at"] = generated_at
    return payload


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "source_selection.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_manifest_rejects_benchmark_semantics(tmp_path):
    payload = valid_manifest(tmp_path)
    payload["documents"][0]["case_id"] = "c01"
    with pytest.raises(ValueError, match="benchmark field"):
        load_source_selection_manifest(_write(tmp_path, payload), body_root=tmp_path)


def test_manifest_rejects_nested_and_unknown_benchmark_keys(tmp_path):
    payload = valid_manifest(tmp_path)
    payload["documents"][0]["tickers"] = ["AAPL"]
    payload["documents"][0]["future"] = {"role": "primary_support"}
    with pytest.raises(ValueError, match="benchmark field"):
        load_source_selection_manifest(_write(tmp_path, payload), body_root=tmp_path)

    payload = valid_manifest(tmp_path)
    payload["documents"][0]["mystery"] = 1
    with pytest.raises(ValueError, match="unknown key"):
        load_source_selection_manifest(_write(tmp_path, payload), body_root=tmp_path)


def test_manifest_identity_excludes_operational_timestamp(tmp_path):
    first = valid_manifest(tmp_path, generated_at="2026-09-18T00:00:00Z")
    second = valid_manifest(tmp_path, generated_at="2026-09-18T01:00:00Z")
    assert source_selection_id(first) == source_selection_id(second)
    loaded_first = load_source_selection_manifest(
        _write(tmp_path, first), body_root=tmp_path
    )
    loaded_second = load_source_selection_manifest(
        _write(tmp_path, second), body_root=tmp_path
    )
    assert loaded_first.source_selection_id == loaded_second.source_selection_id


def test_manifest_rejects_body_mismatch_and_escaping_paths(tmp_path):
    payload = valid_manifest(tmp_path)
    payload["documents"][0]["body_sha256"] = "a" * 64
    with pytest.raises(ValueError, match="hash mismatch"):
        load_source_selection_manifest(_write(tmp_path, payload), body_root=tmp_path)

    for bad_path in ("../escape.txt", "/etc/hostname", "bodies/../../escape.txt"):
        payload = valid_manifest(tmp_path)
        payload["documents"][0]["body_path"] = bad_path
        with pytest.raises(ValueError):
            load_source_selection_manifest(_write(tmp_path, payload), body_root=tmp_path)


def test_manifest_rejects_invalid_class_time_ticker_and_duplicates(tmp_path):
    payload = valid_manifest(tmp_path)
    payload["documents"][0]["source_class"] = "gold_answer"
    with pytest.raises(ValueError, match="invalid source_class"):
        load_source_selection_manifest(_write(tmp_path, payload), body_root=tmp_path)

    payload = valid_manifest(tmp_path)
    payload["documents"][0]["eligible_at"] = "2025-05-01T20:00:00+00:00"
    with pytest.raises(ValueError, match="non-UTC-ISO-Z"):
        load_source_selection_manifest(_write(tmp_path, payload), body_root=tmp_path)

    payload = valid_manifest(tmp_path)
    payload["documents"][0]["tickers"] = ["aapl"]
    with pytest.raises(ValueError, match="invalid ticker"):
        load_source_selection_manifest(_write(tmp_path, payload), body_root=tmp_path)

    payload = valid_manifest(tmp_path)
    duplicate = dict(payload["documents"][0])
    duplicate["canonical_url"] = "https://www.sec.gov/example-10q"
    duplicate.update(_write_body(tmp_path, "bodies/doc-2.txt", "second body"))
    payload["documents"].append(duplicate)
    duplicate["body_path"] = payload["documents"][0]["body_path"]
    duplicate["body_sha256"] = payload["documents"][0]["body_sha256"]
    (tmp_path / "bodies" / "doc-2.txt").unlink()
    with pytest.raises(ValueError, match="duplicate body identity"):
        load_source_selection_manifest(_write(tmp_path, payload), body_root=tmp_path)


def test_empty_sealed_selection_is_a_derivative_only_rebuild(tmp_path):
    """``documents: []`` is a valid seal: rebuild, ingest nothing new."""
    payload = {
        "schema_version": "v1_1_source_selection_v1",
        "selection_policy_id": SELECTION_POLICY_ID,
        "documents": [],
    }
    path = _write(tmp_path, payload)
    manifest = load_source_selection_manifest(path, body_root=tmp_path)
    assert manifest.documents == ()
    assert manifest.source_selection_id == source_selection_id(payload)
    # The empty seal is stable and never depends on the body root.
    other = load_source_selection_manifest(
        _write(tmp_path, payload), body_root=tmp_path / "elsewhere"
    )
    assert other.source_selection_id == manifest.source_selection_id
    assert manifest.source_selection_id != source_selection_id(valid_manifest(tmp_path))


def test_optional_public_document_identity_is_validated(tmp_path):
    payload = valid_manifest(tmp_path)
    document = payload["documents"][0]
    document["filing_accession"] = "0000731766-26-000025"
    document["document_role"] = "exhibit_99_1"
    document["title"] = "UnitedHealth Group Q4 2025 Exhibit 99.1"
    manifest = load_source_selection_manifest(_write(tmp_path, payload), body_root=tmp_path)
    selected = manifest.documents[0]
    assert selected.filing_accession == "0000731766-26-000025"
    assert selected.document_role == "exhibit_99_1"
    assert selected.title == "UnitedHealth Group Q4 2025 Exhibit 99.1"

    for bad_key, bad_value in (
        ("filing_accession", "731766-26-25"),
        ("document_role", "Exhibit 99.1"),
        ("title", "   "),
    ):
        broken = valid_manifest(tmp_path)
        broken["documents"][0][bad_key] = bad_value
        with pytest.raises(ValueError):
            load_source_selection_manifest(_write(tmp_path, broken), body_root=tmp_path)
