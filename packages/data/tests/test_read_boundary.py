"""Consumer read modules must not load collectors or agents at import or use time."""

import subprocess
import sys


def test_market_scope_and_prepared_range_import_without_collectors():
    code = """
import sys
import json
import tempfile
from pathlib import Path
from catalyst_data.read import daily_universe, market_scope, readiness, sec_units, tavily_usage, web_leads
from catalyst_data.storage import runtime_paths
assert market_scope.MAX_SIC_PEERS == 4
assert readiness.PreparedPair('A', '2026-01-01', 1.0).ticker == 'A'
assert isinstance(sec_units.filing_units_disabled(), bool)
assert callable(tavily_usage.used_for_day)
assert callable(web_leads.issuer_matcher)
assert callable(daily_universe.movers_for)
assert callable(daily_universe.universe)
with tempfile.TemporaryDirectory() as temp_dir:
    root = Path(temp_dir)
    manifest = root / 'data/manifests/heldout_universe_v1.json'
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({'sectors': {'tech': ['BBB', 'AAA'], 'other': ['AAA', 'CCC']}}))
    runtime_paths.repository_root = lambda: root
    assert daily_universe.universe() == ['AAA', 'BBB', 'CCC']
assert not any(name.startswith('catalyst_data.ingestion.collectors') for name in sys.modules)
assert not any(name.startswith('catalyst_agents') for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
