import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "restart-if-new-build.sh"


def _run(status: dict, tmp_path: Path) -> tuple[subprocess.CompletedProcess, list[str]]:
    status_path = tmp_path / "daily_pipeline_status.json"
    status_path.write_text(json.dumps(status), encoding="utf-8")
    systemctl = tmp_path / "systemctl"
    systemctl.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CALLS_FILE\"\n", encoding="utf-8")
    systemctl.chmod(0o755)
    calls_file = tmp_path / "calls"
    env = os.environ | {
        "CATALYST_DAILY_PIPELINE_STATUS": str(status_path),
        "CALLS_FILE": str(calls_file),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }
    result = subprocess.run([str(SCRIPT)], env=env, check=False)
    calls = calls_file.read_text(encoding="utf-8").splitlines() if calls_file.exists() else []
    return result, calls


def test_restarts_api_only_after_new_corpus_build(tmp_path: Path) -> None:
    result, calls = _run(
        {
            "daily_status": "COMPLETED",
            "corpus_fts_attempt": {
                "status": "ok",
                "build_id": "new",
                "previous_build_id": "old",
            },
        },
        tmp_path,
    )

    assert result.returncode == 0
    assert calls == ["--no-block try-restart catalyst-api.service"]


def test_does_not_restart_when_corpus_build_was_not_published(tmp_path: Path) -> None:
    result, calls = _run(
        {
            "daily_status": "COMPLETED",
            "corpus_fts": {"status": "ok", "build_id": "new", "previous_build_id": "old"},
            "corpus_fts_attempt": {"status": "skipped", "reason_code": "already_published"},
        },
        tmp_path,
    )

    assert result.returncode == 0
    assert calls == []
