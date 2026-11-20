"""Importing data providers must not write shared rate-budget files before preflight."""
import json
import os
import subprocess
import sys

from catalyst_data.ingestion.ratelimit import SharedBucket


def test_import_does_not_create_bucket_directory(tmp_path):
    env = dict(os.environ, CATALYST_RATELIMIT_DIR=str(tmp_path / "buckets"), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-c", "from catalyst_data.ingestion.collectors import alpaca, market"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "buckets").exists()


def test_lazy_buckets_still_share_one_budget(tmp_path):
    first = SharedBucket("unit", 60, 2, directory=tmp_path, clock=lambda: 100)
    second = SharedBucket("unit", 60, 2, directory=tmp_path, clock=lambda: 100)
    assert first.acquire() == 0
    assert second.acquire() == 0
    assert json.loads(first.path.read_text())["tokens"] == 0
    first.drain()
    assert json.loads(second.path.read_text())["tokens"] == 0


def test_unwritable_state_falls_back_only_when_used(tmp_path):
    blocked = tmp_path / "not_a_directory"
    blocked.write_text("preserve")
    bucket = SharedBucket("unit", 60, 2, directory=blocked)
    assert bucket._local is None
    assert bucket.acquire() == 0
    assert bucket._local is not None
    assert blocked.read_text() == "preserve"
