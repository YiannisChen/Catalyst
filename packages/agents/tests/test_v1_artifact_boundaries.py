"""V1.1 singular artifact ownership boundary tests (M2-11).

Each V1.1 artifact is owned by exactly one package (Final Migration TSD §4.2).
The duplicated-owner scan fails if the same canonical V1.1 class is defined in
two packages. Documented legacy duplicates are recorded BASELINE_ONLY (e.g.
legacy ``RetrievalResultSet`` in ``retrieval/result.py``) and are not V1.1
owners.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# Final Migration TSD §4.2 singular ownership table (V1.1 classes only).
# Includes the complete ownership set exercised by the M2 corrective pass:
# data-core canonical/identity, agents semantic/run artifacts, app runtime
# lifecycle/public event/DTO boundary, eval golden/manifest artifacts.
V1_1_OWNERS: dict[str, str] = {
    "TemporalIdentity": "data-core",
    "CanonicalAsset": "data-core",
    "CanonicalContentVersion": "data-core",
    "CanonicalEvidenceChain": "data-core",
    "TextEvidenceIdentity": "data-core",
    "StructuredEvidenceIdentity": "data-core",
    "DataRuntimeIdentity": "data-core",
    "RetrievalHit": "data-core",
    "RetrievalResultSet": "data-core",
    "MoveProfile": "agents",
    "ResearchTask": "agents",
    "ResearchTaskResult": "agents",
    "EvidenceState": "agents",
    "EvidenceStateItem": "agents",
    "CoverageSummary": "agents",
    "EvidenceAnalystContextPack": "agents",
    "AnalystDecision": "agents",
    "CandidateHypothesis": "agents",
    "EvidenceAssessment": "agents",
    "MissingEvidence": "agents",
    "CorrectiveResearchAction": "agents",
    "CorrectiveResearchBatch": "agents",
    "ClaimPlan": "agents",
    "ValidatedClaimPlan": "agents",
    "WriterInput": "agents",
    "RunManifest": "agents",
    "RunAssuranceRecord": "agents",
    "PublicRunEvent": "app",
    "RunLifecycleStatus": "app",
    "RunEventType": "app",
    "RunDTO": "app",
    "ArtifactRefDTO": "app",
    "GoldenCase": "eval",
    "GoldenCaseLineage": "eval",
    "EvidenceJudgmentV1": "eval",
    "EvalManifest": "eval",
    "EvalOutcome": "eval",
    "EligibleExperiment": "eval",
}

# Documented legacy BASELINE_ONLY definitions that share a V1.1 class name.
BASELINE_ONLY_DEFINITIONS: dict[str, tuple[str, ...]] = {
    "RetrievalResultSet": ("catalyst_data/retrieval/result.py",),
}


def _package_roots() -> dict[str, Path]:
    return {
        "data-core": REPO_ROOT / "packages" / "data-core" / "catalyst_data",
        "agents": REPO_ROOT / "packages" / "agents" / "catalyst_agents",
        "app": REPO_ROOT / "packages" / "app" / "catalyst_app",
        "eval": REPO_ROOT / "packages" / "eval" / "catalyst_eval",
    }


def _class_definitions() -> dict[str, dict[str, list[str]]]:
    """class name -> package -> [relative source paths]."""
    found: dict[str, dict[str, list[str]]] = {}
    for package, root in _package_roots().items():
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            for match in re.finditer(r"^class\s+(\w+)", path.read_text(encoding="utf-8"), re.M):
                name = match.group(1)
                found.setdefault(name, {}).setdefault(package, []).append(rel)
    return found


def test_each_v1_1_artifact_is_importable_from_its_owner() -> None:
    from catalyst_agents.runtime.manifest import RunManifest
    from catalyst_app.events import PublicRunEvent
    from catalyst_data.canonical.identity import DataRuntimeIdentity
    from catalyst_eval.v1_1 import EvalManifest, GoldenCase

    assert DataRuntimeIdentity.__module__.startswith("catalyst_data.")
    assert RunManifest.__module__.startswith("catalyst_agents.")
    assert PublicRunEvent.__module__.startswith("catalyst_app.")
    assert GoldenCase.__module__.startswith("catalyst_eval.")
    assert EvalManifest.__module__.startswith("catalyst_eval.")


def test_no_duplicated_v1_1_artifact_owner() -> None:
    definitions = _class_definitions()
    for name, expected_owner in V1_1_OWNERS.items():
        owners = definitions.get(name, {})
        baseline = BASELINE_ONLY_DEFINITIONS.get(name, ())
        for package, paths in owners.items():
            for path in paths:
                if package == expected_owner:
                    continue
                if path in baseline:
                    continue
                raise AssertionError(
                    f"{name} is owned by {expected_owner} but also defined in "
                    f"{package} at {path} (legacy definitions must be scoped "
                    "BASELINE_ONLY)"
                )
        assert expected_owner in owners, f"{name} has no definition in {expected_owner}"


def test_no_duplicate_v1_1_definition_within_owner_package() -> None:
    """A V1.1 artifact may be defined in exactly one module of its owner
    package; re-exports through __init__ are not class definitions.
    Documented BASELINE_ONLY legacy definitions are excluded."""
    definitions = _class_definitions()
    for name, expected_owner in V1_1_OWNERS.items():
        baseline = BASELINE_ONLY_DEFINITIONS.get(name, ())
        paths = [
            path
            for path in definitions.get(name, {}).get(expected_owner, [])
            if not any(path.endswith(bp) for bp in baseline)
        ]
        assert len(paths) == 1, (
            f"{name} must be defined exactly once in {expected_owner}, found: {paths}"
        )


def test_runtime_lifecycle_and_dto_ownership_complete() -> None:
    """The Final TSD §4.2 table includes app-owned runtime lifecycle, public
    event envelope, and frontend projection DTOs."""
    from catalyst_app.api_dto import (
        ArtifactRefDTO,
        RunDTO,
        WorkbenchProjectionDTO,
    )
    from catalyst_app.events import PublicRunEvent, RunEventType
    from catalyst_app.lifecycle import RunLifecycleStatus

    assert RunLifecycleStatus.__module__ == "catalyst_app.lifecycle"
    assert PublicRunEvent.__module__ == "catalyst_app.events"
    assert RunEventType.__module__ == "catalyst_app.events"
    assert RunDTO.__module__ == "catalyst_app.api_dto"
    assert ArtifactRefDTO.__module__ == "catalyst_app.api_dto"
    assert WorkbenchProjectionDTO.__module__ == "catalyst_app.api_dto"


def test_baseline_only_definitions_remain_documented() -> None:
    """Documented BASELINE_ONLY definitions stay explicit; the legacy live
    retrieval/result.py contract is not a V1.1 owner."""
    assert BASELINE_ONLY_DEFINITIONS["RetrievalResultSet"] == (
        "catalyst_data/retrieval/result.py",
    )
    legacy_path = REPO_ROOT / "packages" / "data-core" / "catalyst_data" / "retrieval" / "result.py"
    assert legacy_path.is_file()
    assert "RetrievalResultSet" in legacy_path.read_text(encoding="utf-8")
