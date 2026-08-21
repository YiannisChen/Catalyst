"""Hidden-gold isolation tests (M2-11).

GoldenCase/EvalManifest and golden fixtures live under catalyst_eval only.
Production package source (data-core, agents, app) never imports catalyst_eval,
and GoldenCase fixtures are not reachable from app/agents production test
fixtures. The pre-existing legacy data-core test dependency on
catalyst_eval.post_import is BASELINE_ONLY and is outside the V1.1 isolation
contract (M8 cleanup).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

_PRODUCTION_SOURCE_ROOTS = (
    REPO_ROOT / "packages" / "data-core" / "catalyst_data",
    REPO_ROOT / "packages" / "agents" / "catalyst_agents",
    REPO_ROOT / "packages" / "app" / "catalyst_app",
)

# Production test fixture roots (app/agents fixtures; data-core tests are out
# of scope because a legacy BASELINE_ONLY test there imports catalyst_eval).
_PRODUCTION_FIXTURE_ROOTS = (
    REPO_ROOT / "packages" / "agents" / "tests" / "fixtures",
    REPO_ROOT / "packages" / "app" / "tests",
)

_IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+catalyst_eval\b", re.MULTILINE)


def test_no_production_package_imports_catalyst_eval() -> None:
    hits: list[str] = []
    for root in _PRODUCTION_SOURCE_ROOTS:
        for path in root.rglob("*.py"):
            if _IMPORT_RE.search(path.read_text(encoding="utf-8")):
                hits.append(str(path.relative_to(REPO_ROOT)))
    assert hits == [], f"production packages import catalyst_eval: {hits}"


def test_golden_fixtures_not_reachable_from_production_fixtures() -> None:
    hits: list[str] = []
    for root in _PRODUCTION_FIXTURE_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if _IMPORT_RE.search(text):
                hits.append(str(path.relative_to(REPO_ROOT)))
            if re.search(r"golden_set", text):
                hits.append(str(path.relative_to(REPO_ROOT)))
    assert hits == [], (
        f"production fixtures reach golden fixtures: {hits}"
    )


def test_golden_case_fixture_directory_lives_under_eval() -> None:
    golden_set = REPO_ROOT / "packages" / "eval" / "golden_set"
    assert golden_set.is_dir(), "eval golden_set directory missing"
    assert golden_set.relative_to(REPO_ROOT).parts[1] == "eval"


def test_production_source_never_names_golden_contracts() -> None:
    """Production source cannot reference GoldenCase/EvalManifest identifiers;
    gold data lives under eval only."""
    hits: list[str] = []
    for root in _PRODUCTION_SOURCE_ROOTS:
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for marker in ("GoldenCase", "EvalManifest", "golden_set"):
                if marker in text:
                    hits.append(
                        f"{path.relative_to(REPO_ROOT)} contains {marker!r}"
                    )
    assert hits == [], f"production source names golden contracts: {hits}"


def test_production_fixtures_never_reference_golden_case_identifiers() -> None:
    hits: list[str] = []
    for root in _PRODUCTION_FIXTURE_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "GoldenCase" in text or "EvalManifest" in text:
                hits.append(str(path.relative_to(REPO_ROOT)))
    assert hits == [], f"production fixtures reference golden identifiers: {hits}"
