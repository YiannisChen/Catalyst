from pathlib import Path
import subprocess
import sys


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_agents_does_not_import_or_depend_on_eval():
    source_hits = []
    for path in (PACKAGE_ROOT / "catalyst_agents").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "catalyst_eval" in text or "from eval" in text or "import eval" in text:
            source_hits.append(path.relative_to(PACKAGE_ROOT).as_posix())

    pyproject = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert source_hits == []
    assert "catalyst-eval" not in pyproject
    assert "catalyst_eval" not in pyproject


def test_agents_public_imports_do_not_touch_eval_with_import_hook():
    code = r'''
import builtins
real_import = builtins.__import__
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "catalyst_eval" or name.startswith("catalyst_eval."):
        raise RuntimeError("catalyst_eval import forbidden")
    return real_import(name, globals, locals, fromlist, level)
builtins.__import__ = guarded_import
import catalyst_agents.graph
import catalyst_agents.attribution
import catalyst_agents.runtime.assurance
import catalyst_agents.trace.schema
import catalyst_agents.trace.version
import catalyst_agents.retrieval.policy
'''
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PACKAGE_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
