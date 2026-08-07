from __future__ import annotations

import re
import pytest


def test_bge_m3_revision_is_pinned():
    from catalyst_data.config import BGE_M3_REVISION

    assert re.fullmatch(r"[0-9a-f]{40}", BGE_M3_REVISION)
    assert BGE_M3_REVISION == "5617a9f61b028005a4858fdac845db406aefb181"


def test_bge_m3_output_dimension():
    from catalyst_data.config import BGE_M3_DIMENSION

    assert BGE_M3_DIMENSION == 1024


def test_embedding_revision_in_manifest():
    from catalyst_data.config import BGE_M3_REVISION
    from catalyst_data.corpus.tokenizer import TOKENIZER_REVISION
    from catalyst_data.retrieval.index_manifest import IndexManifest

    manifest = IndexManifest(
        model_name="BAAI/bge-m3",
        model_revision=BGE_M3_REVISION,
        tokenizer_revision=TOKENIZER_REVISION,
        normalization_mode="l2",
        dtype="float32",
        dimension=1024,
        corpus_manifest_id="a" * 64,
        source_bundle_id="b" * 64,
        snapshot_id="c" * 64,
        probe_report_id="d" * 64,
        postbuild_readiness_id="e" * 64,
        artifact_hashes={
            "vectors.npy": "1" * 64,
            "chunk_ids.json": "2" * 64,
            "lancedb_table": "3" * 64,
        },
        code_revision="0" * 40,
    )
    assert manifest.dimension == 1024
    assert manifest.model_revision == BGE_M3_REVISION
    assert manifest.to_dict()["normalization_mode"] == "l2"


def test_index_manifest_rejects_non_pinned_revisions():
    from catalyst_data.config import BGE_M3_REVISION
    from catalyst_data.corpus.tokenizer import TOKENIZER_REVISION
    from catalyst_data.retrieval.index_manifest import IndexManifest

    kwargs = dict(
        model_name="BAAI/bge-m3", model_revision=BGE_M3_REVISION,
        tokenizer_revision=TOKENIZER_REVISION, normalization_mode="l2",
        dtype="float32", dimension=1024, corpus_manifest_id="a" * 64,
        source_bundle_id="b" * 64, snapshot_id="c" * 64,
        probe_report_id="d" * 64, postbuild_readiness_id="e" * 64,
        artifact_hashes={
            "vectors.npy": "1" * 64,
            "chunk_ids.json": "2" * 64,
            "lancedb_table": "3" * 64,
        },
        code_revision="0" * 40,
    )
    with pytest.raises(ValueError, match="pinned"):
        IndexManifest(**{**kwargs, "model_revision": "0" * 40})
    with pytest.raises(ValueError, match="pinned"):
        IndexManifest(**{**kwargs, "tokenizer_revision": "0" * 40})


def test_index_manifest_binds_approved_identities_and_exact_code_revision():
    from catalyst_data.config import BGE_M3_REVISION
    from catalyst_data.corpus.tokenizer import TOKENIZER_REVISION
    from catalyst_data.retrieval.index_manifest import IndexManifest

    manifest = IndexManifest(
        model_name="BAAI/bge-m3", model_revision=BGE_M3_REVISION,
        tokenizer_revision=TOKENIZER_REVISION, normalization_mode="l2",
        dtype="float32", dimension=1024, corpus_manifest_id="a" * 64,
        source_bundle_id="b" * 64, snapshot_id="c" * 64,
        probe_report_id="d" * 64, postbuild_readiness_id="e" * 64,
        artifact_hashes={
            "vectors.npy": "1" * 64,
            "chunk_ids.json": "2" * 64,
            "lancedb_table": "3" * 64,
        },
        code_revision="0" * 40,
    )
    assert manifest.code_revision == "0" * 40
    assert manifest.to_dict()["code_revision"] == "0" * 40


def test_index_manifest_rejects_placeholder_driver_revision():
    from catalyst_data.config import BGE_M3_REVISION
    from catalyst_data.corpus.tokenizer import TOKENIZER_REVISION
    from catalyst_data.retrieval.index_manifest import IndexManifest

    with pytest.raises(ValueError, match="code revision"):
        IndexManifest(
            model_name="BAAI/bge-m3", model_revision=BGE_M3_REVISION,
            tokenizer_revision=TOKENIZER_REVISION, normalization_mode="l2",
            dtype="float32", dimension=1024, corpus_manifest_id="a" * 64,
            source_bundle_id="b" * 64, snapshot_id="c" * 64,
            probe_report_id="d" * 64, postbuild_readiness_id="e" * 64,
            artifact_hashes={
                "vectors.npy": "1" * 64,
                "chunk_ids.json": "2" * 64,
                "lancedb_table": "3" * 64,
            },
            code_revision="catalyst-b6l-driver-v1",
        )
