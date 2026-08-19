-- Tiny bounded fixture mirroring the promoted B2O snapshot DB table names used
-- by the M1-3 snapshot_identity reader. Never opens the 5.9 GB production DB.
CREATE TABLE corpus_manifest (
    manifest_id TEXT PRIMARY KEY,
    manifest_json TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 0,
    created_at TEXT
);

CREATE TABLE lexical_index_state (
    singleton_id INTEGER PRIMARY KEY,
    schema_version TEXT,
    corpus_manifest_id TEXT,
    mode_served TEXT,
    fallback_reason TEXT,
    row_count INTEGER,
    built_at TEXT,
    lexical_generation_id TEXT,
    lexical_digest TEXT
);

CREATE TABLE corpus_served_chunks (
    chunk_id TEXT PRIMARY KEY,
    document_id TEXT,
    content_text TEXT,
    source_class TEXT,
    available_at TEXT,
    manifest_id TEXT,
    status TEXT
);

CREATE VIRTUAL TABLE corpus_build_chunks_fts USING fts5(chunk_id UNINDEXED, content_text);

CREATE TABLE articles (
    article_id TEXT PRIMARY KEY,
    raw_asset_id TEXT,
    provider TEXT,
    source_type TEXT,
    ticker TEXT,
    title TEXT,
    source_class TEXT
);

CREATE TABLE filings (
    filing_id TEXT PRIMARY KEY,
    cik TEXT,
    ticker TEXT,
    form_type TEXT,
    filed_at TEXT,
    accession_number TEXT,
    raw_asset_id TEXT
);

INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current, created_at) VALUES
('3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc',
 '{"certified_snapshot_identity":"7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49","inventory_row_count":295506,"inventory_storage":"corpus_build_chunks","tokenizer_model_id":"BAAI/bge-m3","tokenizer_revision":"5617a9f61b028005a4858fdac845db406aefb181"}',
 1, '2026-08-02T16:30:44.648224+00:00'),
('50d68fc76208840cb55c809a95c3f74a9e74e125dd291633212a70731559f2a4',
 '{"certified_snapshot_identity":"e38ab4294b43830fa601f0b5f70c937cd7bb6a49dff9e483b169ce7c3053b2d4","inventory_row_count":295506}',
 0, '2026-08-01T13:43:25.797186+00:00');

INSERT INTO lexical_index_state
(singleton_id, schema_version, corpus_manifest_id, mode_served, fallback_reason, row_count, built_at, lexical_generation_id, lexical_digest)
VALUES
(1, '1.0.0', '3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc', 'fts5', NULL, 295506,
 '2026-08-02T16:30:44.648224+00:00', '3839ca95828ccc95b25f18b4ec9b0600fdf5d5abb6fa8dcf903a806392fed51c',
 '54d547ff53e8e5e2e8a5aad9822c264d4cf0917781b0c99183e44611fa7d7a4e');

INSERT INTO corpus_served_chunks (chunk_id, document_id, content_text, source_class, available_at, manifest_id, status)
VALUES
('chunk-0001', 'doc-0001', 'fixture body one', 'reported_news', '2026-01-01T00:00:00Z', '3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc', 'active'),
('chunk-0002', 'doc-0002', 'fixture body two', 'issuer_disclosure', '2026-01-02T00:00:00Z', '3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc', 'active');

INSERT INTO corpus_build_chunks_fts (chunk_id, content_text) VALUES
('chunk-0001', 'fixture body one'),
('chunk-0002', 'fixture body two'),
('chunk-0003', 'fixture body three');

INSERT INTO articles (article_id, raw_asset_id, provider, source_type, ticker, title, source_class) VALUES
('art-0001', 'raw-0001', 'test-provider', 'news', 'AAPL', 'First fixture article', 'reported_news'),
('art-0002', 'raw-0002', 'test-provider', 'news', 'MSFT', 'Second fixture article', 'reported_news');

INSERT INTO filings (filing_id, cik, ticker, form_type, filed_at, accession_number, raw_asset_id) VALUES
('fil-0001', '0000320193', 'AAPL', '10-Q', '2026-01-15T00:00:00Z', '0000320193-26-000001', 'raw-0003');
