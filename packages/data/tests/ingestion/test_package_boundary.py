"""Data-core package direction and temporal identity contract."""
from pathlib import Path

import pytest
from catalyst_data.canonical.temporal import build_temporal_identity


def test_canonical_ingestion_namespace_is_available() -> None:
    from catalyst_data import ingestion
    from catalyst_data.ingestion import scope

    assert ingestion.scope is scope


def test_data_core_imports_no_agents_or_app() -> None:
    package_root = Path(__file__).resolve().parents[2] / "catalyst_data"
    forbidden_imports = (
        "import catalyst_agents",
        "from catalyst_agents",
        "import catalyst_app",
        "from catalyst_app",
    )
    offenders = []
    for path in sorted(package_root.rglob("*.py")):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(pattern in line for pattern in forbidden_imports):
                offenders.append(f"{path.relative_to(package_root)}:{line_number}: {line.strip()}")
    assert not offenders, "Data-core imports upper-layer packages:\n" + "\n".join(offenders)


def test_temporal_identity() -> None:
    identity = build_temporal_identity("2026-07-14")
    assert identity.session_date == "2026-07-14"
    assert identity.cutoff_at == identity.session_close_at


def test_missing_session_date() -> None:
    with pytest.raises(ValueError, match="session_date is required for temporal identity"):
        build_temporal_identity("")
