from __future__ import annotations

import os
import re
import runpy
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _run(command, *, cwd, env=None):
    return subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)


def test_deploy_scripts_pass_bash_syntax_check():
    for script in (ROOT / "deploy/deploy.sh", ROOT / "deploy/deploy_collectors.sh",
                   ROOT / "deploy/ops/deploy_sandbox_check", ROOT / "deploy/ops/collector_timer_want",
                   ROOT / "deploy/ops/catalyst-backup"):
        result = _run(["bash", "-n", str(script)], cwd=ROOT)
        assert result.returncode == 0, result.stderr


def test_deploy_guards_match_old_and_current_process_paths():
    for script, processes in (
        ("deploy/deploy.sh", (
            "catalyst_data.m9.daily",
            "catalyst_data.ingestion.daily",
            "catalyst_data.ingestion.ops.daily",
        )),
        ("deploy/deploy_collectors.sh", (
            "catalyst_data.m9.alpaca",
            "catalyst_data.ingestion.alpaca",
            "catalyst_data.ingestion.collectors.alpaca",
        )),
    ):
        text = (ROOT / script).read_text(encoding="utf-8")
        pattern = re.search(r"\[c\]atalyst_data[^']+", text)
        assert pattern is not None
        assert all(re.search(pattern.group(), process) for process in processes)


def test_daily_and_health_units_use_the_current_data_package():
    for unit in (
        ROOT / "deploy/api/catalyst-daily.service",
        ROOT / "deploy/ops/catalyst-health.service",
    ):
        text = unit.read_text()
        assert "PYTHONPATH=/opt/catalyst/repo/packages/data" in text
        assert "-m catalyst_data.ingestion." in text
        assert "catalyst_data.m9" not in text


def test_sandbox_backup_root_preserves_legacy_default_and_honors_override(monkeypatch, tmp_path):
    module = runpy.run_path(str(ROOT / "deploy/ops/deploy_sandbox_check.py"))
    monkeypatch.delenv("CATALYST_RUNTIME_DATA_DIR", raising=False)
    assert module["daily_data_root"]() == Path("/opt/catalyst/repo/data/m9")

    monkeypatch.setenv("CATALYST_RUNTIME_DATA_DIR", "runtime-state")
    assert module["daily_data_root"]() == Path("/opt/catalyst/repo/runtime-state")
    monkeypatch.setenv("CATALYST_RUNTIME_DATA_DIR", str(tmp_path / "runtime-state"))
    assert module["daily_data_root"]() == tmp_path / "runtime-state"


def test_backup_root_validation_fails_closed_without_body_store(tmp_path):
    script = (ROOT / "deploy/ops/catalyst-backup").read_text()
    block = script.split("# BEGIN runtime data root validation\n", 1)[1].split(
        "# END runtime data root validation", 1
    )[0]

    valid_root = tmp_path / "valid"
    valid_root.mkdir()
    (valid_root / "news_bodies_v2.sqlite3").touch()
    result = subprocess.run(
        ["bash", "-c", block + '\nprintf "%s\\n" "$RUNTIME_DATA_DIR"'],
        cwd=ROOT,
        env={**os.environ, "CATALYST_RUNTIME_DATA_DIR": str(valid_root)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(valid_root)

    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    result = subprocess.run(
        ["bash", "-c", block],
        cwd=ROOT,
        env={**os.environ, "CATALYST_RUNTIME_DATA_DIR": str(empty_root)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "runtime body store missing" in result.stderr


def _clean_checkout(tmp_path):
    repo = tmp_path / "repo"
    (repo / "deploy/ops").mkdir(parents=True)
    (repo / "packages/data").mkdir(parents=True)
    for rel in ("deploy/deploy.sh", "deploy/deploy_collectors.sh", "deploy/ops/collector_timer_want"):
        destination = repo / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / rel).read_bytes())
    for name in ("monitor-gate", "catalyst-backup", "catalyst-job", "catalyst-shadow", "shadow_compare.py"):
        (repo / "deploy/ops" / name).write_text("#!/bin/sh\nexit 0\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text("#!/bin/bash\nprintf '%s' \"${!#}\" > \"$CAPTURE_SSH\"\nexit 0\n")
    fake_ssh.chmod(0o755)
    fake_python = fake_bin / "python3"
    fake_python.write_text("#!/bin/sh\nexit 0\n")
    fake_python.chmod(0o755)
    _run(["git", "init", "-q"], cwd=repo)
    _run(["git", "config", "user.email", "test@example.invalid"], cwd=repo)
    _run(["git", "config", "user.name", "Test"], cwd=repo)
    _run(["git", "add", "."], cwd=repo)
    assert _run(["git", "commit", "-qm", "fixture"], cwd=repo).returncode == 0
    bare = tmp_path / "remote.git"
    _run(["git", "init", "--bare", "-q", str(bare)], cwd=tmp_path)
    _run(["git", "remote", "add", "rn", str(bare)], cwd=repo)
    return repo, fake_bin, fake_ssh


@pytest.mark.parametrize("mode", ["--code-only", "--enable-timers"])
def test_collectors_modes_send_expected_remote_script_without_enabling_retry(tmp_path, mode):
    repo, fake_bin, fake_ssh = _clean_checkout(tmp_path)
    capture = tmp_path / "remote-script"
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CATALYST_SSH": str(fake_ssh),
        "CATALYST_DEPLOY_HOST": "user@host.example.test",
        "CAPTURE_SSH": str(capture),
    }

    result = _run(["bash", "deploy/deploy_collectors.sh", mode, "HEAD"], cwd=repo, env=env)

    assert result.returncode == 0, result.stderr
    remote = capture.read_text()
    if mode == "--code-only":
        assert "systemctl enable" not in remote
        assert "systemctl disable" not in remote
        assert "systemctl daemon-reload" not in remote
        assert "cp deploy/ops/catalyst-" not in remote
        assert 'if [[ ! -d /opt/catalyst/collectors/.git ]]' in remote
        assert "git clone -q /opt/catalyst/catalyst.git /opt/catalyst/collectors" in remote
        assert 'test "$(git rev-parse HEAD)" = ' in remote
    else:
        assert "collector_timer_want --stdin" in remote
        assert "systemctl enable -q --now catalyst-resilience-retry.timer" not in remote
        assert "enable -q --now $want" in remote
        assert "is-enabled catalyst-resilience-retry.timer" in remote
        assert "disable --now catalyst-resilience-retry.timer" in remote


def test_units_yes_does_not_install_resilience_retry(tmp_path):
    repo, fake_bin, fake_ssh = _clean_checkout(tmp_path)
    capture = tmp_path / "remote-script"
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CATALYST_SSH": str(fake_ssh),
        "CATALYST_DEPLOY_HOST": "user@host.example.test",
        "CAPTURE_SSH": str(capture),
    }

    result = _run(["bash", "deploy/deploy_collectors.sh", "--units", "HEAD", "--yes"], cwd=repo, env=env)

    assert result.returncode == 0, result.stderr
    remote = capture.read_text()
    assert 'echo "=== $name (未安装) ==="' in remote
    assert 'for unit in deploy/collect/catalyst-collect-*.service deploy/collect/catalyst-collect-*.timer deploy/ops/catalyst-health.service deploy/ops/catalyst-health.timer deploy/ops/catalyst-backup.service deploy/ops/catalyst-backup.timer; do' in remote
    assert "cp \"$unit\" /etc/systemd/system/" in remote
    assert 'deploy/ops/catalyst-resilience-retry.service deploy/ops/catalyst-resilience-retry.timer; do cp' not in remote


@pytest.mark.parametrize("script", ["deploy/deploy.sh", "deploy/deploy_collectors.sh"])
def test_deploy_scripts_refuse_dirty_worktree(tmp_path, script):
    repo, fake_bin, fake_ssh = _clean_checkout(tmp_path)
    (repo / "dirty").write_text("uncommitted\n")
    env = os.environ | {"PATH": f"{fake_bin}:{os.environ['PATH']}", "CATALYST_SSH": str(fake_ssh)}
    args = ["bash", script, "HEAD"] if script.endswith("deploy.sh") else ["bash", script, "--code-only", "HEAD"]

    result = _run(args, cwd=repo, env=env)

    assert result.returncode != 0
    assert "worktree is not clean" in result.stderr + result.stdout


def _sandbox_fakes(tmp_path):
    fake_bin = tmp_path / "sandbox-bin"
    fake_bin.mkdir()
    fixture = tmp_path / "target-fixture"
    (fixture / "deploy/ops").mkdir(parents=True)
    (fixture / "packages/data").mkdir(parents=True)
    (fixture / "deploy/ops/deploy_sandbox_check.py").write_text("# fixture\n")
    (fixture / "deploy/ops/collector_timer_want").write_bytes(
        (ROOT / "deploy/ops/collector_timer_want").read_bytes())
    (fixture / "deploy/ops/collector_timer_want").chmod(0o755)
    git = fake_bin / "git"
    git.write_text('''#!/bin/bash
set -e
case "$*" in
  *"rev-parse --verify"*) echo "${FAKE_RESOLVED:-$TARGET_SHA}" ;;
  *"clone -q --no-checkout"*) mkdir -p "$5"; cp -R "$GIT_FIXTURE"/. "$5"/ ;;
  *"checkout -q --detach"*) [[ -z "${FAKE_CHECKOUT_FAIL:-}" ]] ;;
  *"rev-parse HEAD"*) echo "$TARGET_SHA" ;;
  *) exit 0 ;;
esac
''')
    git.chmod(0o755)
    python = fake_bin / "python"
    python.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ["PYTHON_LOG"], "a") as f: f.write(" ".join(args) + "\\n")
if os.environ.get("FAIL_PY_TOKEN") and os.environ["FAIL_PY_TOKEN"] in " ".join(args): sys.exit(7)
if "timers" in args: print("catalyst-collect-demo\\ncatalyst-health\\ncatalyst-backup")
elif "--json" in args:
    path = pathlib.Path(args[args.index("--json") + 1]); path.write_text(json.dumps({"stores": []}))
elif len(args) >= 3 and args[1] == "backup":
    pathlib.Path(args[3], "archive").mkdir(parents=True, exist_ok=True)
    pathlib.Path(args[3], "daily").mkdir(parents=True, exist_ok=True)
''')
    python.chmod(0o755)
    systemctl = fake_bin / "systemctl"
    systemctl.write_text('''#!/bin/bash
echo "$*" >> "$SYSTEMCTL_LOG"
if [[ "$1" == list-unit-files ]]; then
  echo "catalyst-collect-demo.timer enabled enabled"
  echo "catalyst-health.timer enabled enabled"
  echo "catalyst-backup.timer enabled enabled"
  echo "catalyst-daily.timer enabled enabled"
  echo "catalyst-shadow-collectors.timer enabled enabled"
  echo "catalyst-resilience-retry.timer disabled disabled"
fi
if [[ "$1" == is-enabled || "$1" == is-active ]]; then [[ -n "${RETRY_ENABLED:-}" ]]; exit $?; fi
''')
    systemctl.chmod(0o755)
    shell = ROOT / "deploy/ops/deploy_sandbox_check"
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TARGET_SHA": "a" * 40,
        "GIT_FIXTURE": str(fixture),
        "CATALYST_SANDBOX_PYTHON": str(python),
        "PYTHON_LOG": str(tmp_path / "python.log"),
        "SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"),
    }
    return shell, env


def test_sandbox_check_fails_when_resolved_sha_differs(tmp_path):
    script, env = _sandbox_fakes(tmp_path)
    env["FAKE_RESOLVED"] = "b" * 40

    result = _run(["bash", str(script), "a" * 40, str(tmp_path / "job")], cwd=tmp_path, env=env)

    assert result.returncode == 1
    report = (tmp_path / "job/report.md").read_text()
    assert "Target checkout and SHA verification" in report
    assert "Final status: FAIL" in report


def test_sandbox_check_fails_when_target_checkout_fails(tmp_path):
    script, env = _sandbox_fakes(tmp_path)
    env["FAKE_CHECKOUT_FAIL"] = "1"

    result = _run(["bash", str(script), "a" * 40, str(tmp_path / "job")], cwd=tmp_path, env=env)

    assert result.returncode == 1
    report = (tmp_path / "job/report.md").read_text()
    assert "Target checkout and SHA verification" in report
    assert "FAIL" in report
    assert "Final status: FAIL" in report


def test_sandbox_check_fails_when_automated_step_fails(tmp_path):
    script, env = _sandbox_fakes(tmp_path)
    env["FAIL_PY_TOKEN"] = "catalyst_data.ingestion.sources timers"

    result = _run(["bash", str(script), "a" * 40, str(tmp_path / "job")], cwd=tmp_path, env=env)

    assert result.returncode == 1
    report = (tmp_path / "job/report.md").read_text()
    assert "3. Target sources timers vs enabled systemd timers" in report
    assert "FAIL" in report


def test_sandbox_check_passes_without_mutating_systemd(tmp_path):
    script, env = _sandbox_fakes(tmp_path)

    result = _run(["bash", str(script), "a" * 40, str(tmp_path / "job"), "--rollback-sha", "c" * 40],
                  cwd=tmp_path, env=env)

    assert result.returncode == 0, result.stderr + result.stdout
    report = (tmp_path / "job/report.md").read_text()
    assert "Final status: PASS" in report
    assert "3. Target sources timers" in report
    assert "5. Daily scheduler" in report and "6. Offline health" in report and "7. In-flight" in report
    calls = (tmp_path / "systemctl.log").read_text().splitlines()
    assert calls == ["list-unit-files catalyst-*.timer --no-legend",
                     "is-enabled catalyst-resilience-retry.timer",
                     "is-active catalyst-resilience-retry.timer"]
    assert not any(call.split()[0] in {"enable", "disable", "start", "stop", "restart"} for call in calls)


@pytest.mark.parametrize("args", [["--units", "HEAD", "--yes"], ["--enable-timers", "HEAD"]])
def test_units_and_timers_refuse_when_remote_checkout_is_not_target(tmp_path, args):
    repo, fake_bin, fake_ssh = _clean_checkout(tmp_path)
    capture = tmp_path / "remote-script"
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CATALYST_SSH": str(fake_ssh),
        "CATALYST_DEPLOY_HOST": "user@host.example.test",
        "CAPTURE_SSH": str(capture),
    }
    sha = _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()

    result = _run(["bash", "deploy/deploy_collectors.sh", *args], cwd=repo, env=env)

    assert result.returncode == 0, result.stderr
    remote = capture.read_text()
    guard = f'test "$(git -C /opt/catalyst/collectors rev-parse HEAD)" = \'{sha}\' || {{'
    assert guard in remote
    # The guard runs before any unit is copied or timer enabled.
    assert remote.index(guard) < min(i for i in (remote.find("cp \"$unit\""), remote.find("systemctl enable"),
                                                 remote.find("for unit in")) if i >= 0)


def test_sandbox_check_fails_when_resilience_retry_timer_is_enabled(tmp_path):
    script, env = _sandbox_fakes(tmp_path)
    env["RETRY_ENABLED"] = "1"

    result = _run(["bash", str(script), "a" * 40, str(tmp_path / "job")], cwd=tmp_path, env=env)

    assert result.returncode == 1
    report = (tmp_path / "job/report.md").read_text()
    assert "Final status: FAIL" in report
