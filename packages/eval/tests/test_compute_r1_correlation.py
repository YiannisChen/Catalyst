from pathlib import Path
import importlib.util

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = PROJECT_ROOT / "scripts/reports/compute_r1_correlation.py"
SPEC = importlib.util.spec_from_file_location("compute_r1_correlation", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MODULE)
compute_metrics = MODULE.compute_metrics


def test_compute_metrics_returns_pearson_spearman_bucket():
    metrics = compute_metrics(
        catalyst_rows=[
            {"case_id": "g001", "chunk_id": "c1", "relevance": 0.9},
            {"case_id": "g001", "chunk_id": "c2", "relevance": 0.1},
        ],
        external_rows=[
            {"case_id": "g001", "chunk_id": "c1", "relevance": 0.8},
            {"case_id": "g001", "chunk_id": "c2", "relevance": 0.2},
        ],
    )
    assert set(metrics) >= {"pearson", "spearman", "bucket_agreement"}
    assert metrics["pearson"] > 0
    assert metrics["spearman"] > 0
