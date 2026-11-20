"""Manual daily backfill cannot resolve/open production stores without input preflight."""
import pytest
from catalyst_data.ingestion.input_manifest import PreflightError
from catalyst_data.ingestion.ops import daily


@pytest.mark.parametrize("arguments", [["run", "--start", "2026-10-22"], ["retry", "--job", "x"], ["deferred"]])
def test_manual_daily_checks_before_resolving_stores(arguments, monkeypatch):
    monkeypatch.delenv("CATALYST_INPUT_MANIFEST", raising=False)
    monkeypatch.setattr(daily.Paths, "from_env", lambda: pytest.fail("read configuration before preflight"))
    with pytest.raises(PreflightError):
        daily.main(arguments)
