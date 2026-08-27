# Archived: Catalyst v1.0.3 MCJ semantic production path (M5-11)

These files powered the sealed Miner-Critic-Judge (MCJ) production semantic
path before the V1.1 semantic cutover (M5). They are archived — not deleted —
so the sealed M1 baseline remains reproducible by code revision + artifacts.
The V1.1 production graph (`catalyst_agents.graph.run_v1_graph`) is the only
selectable production graph. Legacy behavior remains readable only through
historical readers and the sealed baseline instrument (Git history, tags,
reports, `packages/eval/catalyst_eval/baseline/*`).

Provenance: archived from `packages/agents/catalyst_agents` at M5-11 of the
V1.1 migration (branch `v1.1/m5-semantic-cutover`), preserving the exact file
contents via `git mv` (no content changes).

License: MIT (same as the repository; see repository LICENSE).

Files:

- `critic.py` — legacy Critic node (replaced by `nodes/evidence_analyst.py`).
- `judge.py` — legacy Judge node (deleted; responsibilities moved to Analyst,
  ClaimPlan, Writer, assurance).
- `miner.py` — legacy Miner node (retrieval adapter reused by ResearchExecutor).
- `validator.py` — legacy LLM Validator (deterministic gates moved to
  `attribution/claim_validation.py`; LLM repair deleted).
- `critic.md`, `judge.md` — legacy semantic prompts.
- `hypothesis.py`, `output_status.py`, `ranking.py`, `gates.py`,
  `relationships.py` — legacy semantic helpers only reachable from the
  archived nodes.
- `backoff.py` — legacy `invoke_with_retries` (`MAX_RETRIES=3`), baseline-only;
  the V1.1 retry seam is `runtime/provider_capability.py`.
