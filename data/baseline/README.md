# V1.1 baseline data retention policy

- Reports under `data/baseline/reports/` are permanent and committed.
- Stores, DBs, LanceDB trees, embeddings, and `run_reports` are never committed.
- M1 report `v1_1_baseline_621375bc.json` is explicitly NON-COMPARABLE because
  Q-002 is unrecovered (`promoted_env_recovered=false`). Do not cite it as a
  comparable seal.
