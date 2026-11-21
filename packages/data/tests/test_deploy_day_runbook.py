from __future__ import annotations

import ipaddress
import os
import runpy
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.skipif(
    not (REPO / "docs/runbook.md").is_file(),
    reason="private operations runbook is not part of the public export",
)


def test_cloudflare_checker_reports_added_and_missing_from_local_fixtures(tmp_path):
    source = tmp_path / "cloudflare_ips.txt"
    source.write_text("# managed list\n10.0.0.0/16\n203.0.113.0/24\n2001:db8:0::/48\n")
    v4 = tmp_path / "ips-v4"
    v4.write_text("".join(f"10.{i}.0.0/16\n" for i in range(10)))
    v6 = tmp_path / "ips-v6"
    v6.write_text("".join(f"2001:db8:{i}::/48\n" for i in range(5)))

    result = subprocess.run(
        [sys.executable, str(REPO / "deploy/ops/check_cloudflare_ips.py"),
         "--source", str(source), "--v4-file", str(v4), "--v6-file", str(v6)],
        text=True, capture_output=True,
    )

    assert result.returncode == 1
    assert "ADDED 10.1.0.0/16" in result.stdout
    assert "MISSING 203.0.113.0/24" in result.stdout
    assert "Cloudflare ranges differ" in result.stderr
    assert source.read_text() == "# managed list\n10.0.0.0/16\n203.0.113.0/24\n2001:db8:0::/48\n"


def test_cloudflare_checker_write_updates_list_and_nginx_include_from_fixtures(tmp_path):
    source = tmp_path / "cloudflare_ips.txt"
    source.write_text("# managed list\n192.0.2.0/24\n")
    v4 = tmp_path / "ips-v4"
    v4.write_text("".join(f"10.{i}.0.0/16\n" for i in range(10)))
    v6 = tmp_path / "ips-v6"
    v6.write_text("".join(f"2001:db8:{i}::/48\n" for i in range(5)))
    include = tmp_path / "allow.conf"
    include.write_text("old allow config\n")
    previous_source = source.read_text()
    previous_include = include.read_text()

    result = subprocess.run(
        [sys.executable, str(REPO / "deploy/ops/check_cloudflare_ips.py"),
         "--source", str(source), "--include-output", str(include), "--v4-file", str(v4),
         "--v6-file", str(v6), "--write"], text=True, capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    configured_ranges = [str(ipaddress.ip_network(line.strip(), strict=False))
                         for line in source.read_text().splitlines()
                         if line.strip() and not line.lstrip().startswith("#")]
    generated = include.read_text()
    assert "geo $realip_remote_addr $catalyst_cloudflare_peer {" in generated
    geo_block = generated.split(
        "geo $realip_remote_addr $catalyst_cloudflare_peer {", 1
    )[1].split("}", 1)[0]
    assert "default 0;" in geo_block
    for network in configured_ranges:
        assert f"set_real_ip_from {network};" in generated
        assert f"{network} 1;" in geo_block
    assert "allow " not in generated
    assert "deny all;" not in generated
    source_backups = list(tmp_path.glob("cloudflare_ips.txt.bak.*"))
    include_backups = list(tmp_path.glob("allow.conf.bak.*"))
    assert len(source_backups) == len(include_backups) == 1
    assert source_backups[0].read_text() == previous_source
    assert include_backups[0].read_text() == previous_include


def test_cloudflare_write_rejects_empty_v6_without_changing_either_file(tmp_path):
    source = tmp_path / "cloudflare_ips.txt"
    source.write_text("# unchanged\n10.0.0.0/8\n")
    include = tmp_path / "allow.conf"
    include.write_text("unchanged include\n")
    v4 = tmp_path / "ips-v4"
    v4.write_text("".join(f"10.{i}.0.0/16\n" for i in range(10)))
    v6 = tmp_path / "ips-v6"
    v6.write_text("# empty response\n")
    before_source, before_include = source.read_bytes(), include.read_bytes()

    result = subprocess.run(
        [sys.executable, str(REPO / "deploy/ops/check_cloudflare_ips.py"),
         "--source", str(source), "--include-output", str(include), "--v4-file", str(v4),
         "--v6-file", str(v6), "--write"], text=True, capture_output=True,
    )

    assert result.returncode != 0
    assert "v6" in result.stderr.lower()
    assert source.read_bytes() == before_source
    assert include.read_bytes() == before_include
    assert not list(tmp_path.glob("*.bak.*"))


def test_seed_corpus_dry_run_lists_integrity_checks_without_executing(tmp_path):
    source = tmp_path / "source.sqlite3"
    conn = sqlite3.connect(source)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE source_marker (value TEXT)")
    conn.execute("INSERT INTO source_marker VALUES ('untouched')")
    conn.commit()
    source_wal = Path(str(source) + "-wal")
    assert source_wal.exists() and source_wal.stat().st_size > 0
    source_before = source.read_bytes()
    wal_before = source_wal.read_bytes()
    result = subprocess.run(
        ["bash", str(REPO / "deploy/ops/seed_corpus.sh"), "--host", "root@example.invalid",
         "--source", str(source), "--remote-path",
         "/opt/catalyst/repo/data/m9/catalyst_corpus.sqlite3"],
        text=True, capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    for phrase in ("lsof", "backup API", "wal_checkpoint(TRUNCATE)", "corpus_manifest", "lexical_index_state",
                   "corpus_publication_builds", "rsync --partial --checksum", "sha256sum",
                   "mode=ro&immutable=1", "PRAGMA quick_check", "CATALYST_CORPUS_MIN_FREE_GB", "mv --no-clobber",
                   "chown root:root", "systemctl stop catalyst-daily.timer",
                   "f14324922ff6a0618afb2f9cb2f1120b2791f812c2c1072048d8565fa7dcc8e9"):
        assert phrase in result.stdout
    rsync_line = next(line for line in result.stdout.splitlines() if "rsync --partial --checksum" in line)
    assert ".catalyst-corpus-seed-" in rsync_line
    assert "catalyst_corpus.sqlite3" in rsync_line
    assert str(source) not in rsync_line
    assert "After the code switch and health checks" in result.stdout
    assert source.read_bytes() == source_before
    assert source_wal.read_bytes() == wal_before
    assert not list(tmp_path.glob(".catalyst-corpus-seed.*"))
    conn.close()


def test_seed_finalize_does_not_start_daily_timer_automatically():
    namespace = runpy.run_path(str(REPO / "deploy/ops/seed_corpus.py"))
    start_mentions = [line.strip() for line in namespace["FINALIZE"].splitlines()
                      if "systemctl start catalyst-daily.timer" in line]
    assert start_mentions
    assert all(line.startswith('echo "') for line in start_mentions)


def test_seed_corpus_refuses_existing_remote_incoming_without_overwrite(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text("#!/bin/sh\nhost=$1; shift\nexec /bin/bash -c \"$1\"\n")
    fake_ssh.chmod(0o755)
    env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}")
    source = tmp_path / "source.sqlite3"
    source.touch()
    incoming = tmp_path / "remote/data/catalyst_corpus.sqlite3.incoming.older"
    incoming.parent.mkdir(parents=True)
    incoming.write_text("existing incoming placeholder")

    result = subprocess.run(
        ["bash", str(REPO / "deploy/ops/seed_corpus.sh"), "--execute", "--host", "root@example.invalid",
         "--source", str(source), "--remote-path",
         str(incoming).removesuffix(".incoming.older")],
        text=True, capture_output=True, env=env,
    )

    assert result.returncode != 0
    assert "incoming" in result.stderr.lower()
    assert "rename" in result.stderr.lower()
    assert incoming.read_text() == "existing incoming placeholder"
