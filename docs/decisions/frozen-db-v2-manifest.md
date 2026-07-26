# Frozen Evaluation Fixture v2

- Status: retained compatibility fixture, not the evolving product corpus
- Path: `data/catalyst_eval_frozen_v2.db`
- Current local SHA-256: `0d97a7ec61b6ec8fb5f9263b0b37b0efc9755812739d7afead7f720c3567e8dd`
- Current `clean_assets` count: 13,474

The original byte-level canonical artifact is no longer available locally. The current fixture has matching recorded source counts and content-level checks but must not be described as byte-identical to the lost artifact. It remains useful for compatibility, deterministic read tests, and zero-network fixtures; it is not the evolving Dev corpus and is not the source for new provider coverage.

Rules:

1. never open this path writable;
2. reject its realpath before a writable SQLite connection or PRAGMA;
3. never run schema migrations against it;
4. use disposable copies for guard tests;
5. bind any result pack to the exact SHA it actually consumed;
6. do not silently re-pin or overwrite the fixture.

The evolving product corpus is `data/catalyst_dev_ws4b.db`. Backend plans must verify both database SHAs before and after tests that are expected to be read-only.
