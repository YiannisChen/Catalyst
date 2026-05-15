from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_r1_runbook_contains_cloud_experiment_semantics():
    text = (PROJECT_ROOT / "docs/plans/2026-05-15-r1-cloud-execution-runbook.md").read_text(encoding="utf-8")
    assert "Experiment 1" in text
    assert "Experiment 2" in text
    assert "Experiment 3" in text
    assert "Do NOT run experiments on local machine" in text
    assert "p1_ablation.py" in text
    assert "run_r1_external_baseline.py local/mock only" in text
    assert "Pearson >= 0.70" in text
    assert "Spearman >= 0.70" in text
    assert "Bucket agreement >= 0.80" in text


def test_runbook_contains_concrete_real_exp1_command():
    text = (PROJECT_ROOT / "docs/plans/2026-05-15-r1-cloud-execution-runbook.md").read_text(encoding="utf-8")
    assert "--real" in text
    assert "AIHUBMIX_API_KEY" in text
    assert "r1_sonnet_grades.jsonl" in text
    assert "cloud real grading command placeholder" not in text
