"""Corpus embedder grain and legacy script quarantine."""

from pathlib import Path

from catalyst_data.retrieval.embedder import EMBED_ROWS_SQL, fake_vector_for_tests
from catalyst_data import config


def test_legacy_script_help_mentions_legacy_frozen_eval():
    script = Path(__file__).resolve().parents[1] / "scripts" / "build_embeddings_gpu.py"
    text = script.read_text()
    assert "LEGACY_FROZEN_EVAL" in text


def test_embedder_sql_uses_selected_corpus_relation():
    assert "{chunks_relation}" in EMBED_ROWS_SQL
    assert "clean_assets" not in EMBED_ROWS_SQL


def test_fake_vectors_only_in_test_helper_not_export_api():
    import inspect
    from catalyst_data.retrieval import source_bundle

    src = inspect.getsource(source_bundle)
    assert "fake_vector" not in src
    v = fake_vector_for_tests("seed", dim=8)
    assert v.shape == (8,)


def test_bge_pins_in_config():
    assert config.BGE_M3_REVISION == "5617a9f61b028005a4858fdac845db406aefb181"
    assert config.BGE_RERANKER_REVISION == "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
    assert config.BGE_M3_DIMENSION == 1024
