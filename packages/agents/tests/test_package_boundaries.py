from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_agents_does_not_import_or_depend_on_eval():
    source_hits = []
    for path in (PACKAGE_ROOT / "catalyst_agents").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "catalyst_eval" in text:
            source_hits.append(path.relative_to(PACKAGE_ROOT).as_posix())

    pyproject = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert source_hits == []
    assert "catalyst-eval" not in pyproject
