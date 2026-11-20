import importlib.util
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def test_deploy_scripts_have_valid_bash_and_dry_run(tmp_path):
    deploy = REPO / "deploy/deploy_api.sh"
    subprocess.run(["bash", "-n", str(deploy)], check=True)
    subprocess.run(["bash", "-n", str(REPO / "deploy/ops/cloudflare_ufw.sh")], check=True)
    result = subprocess.run(["bash", str(deploy), "--dry-run", "--root", str(tmp_path)],
                            text=True, capture_output=True, check=True)
    assert "nginx -t" in result.stdout
    assert not (tmp_path / "etc/nginx/conf.d/catalyst-api.conf").exists()


def test_cloudflare_include_generator_uses_all_configured_ranges(tmp_path):
    path = REPO / "deploy/ops/generate_cloudflare_include.py"
    spec = importlib.util.spec_from_file_location("cf_generator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / "allow.conf"
    module.generate(REPO / "deploy/nginx/cloudflare_ips.txt", output)
    rendered = output.read_text()
    assert "set_real_ip_from 173.245.48.0/20;" in rendered
    assert "allow 2400:cb00::/32;" in rendered
    assert rendered.rstrip().endswith("deny all;")


def test_nginx_strict_limits_cover_live_and_data_write_routes():
    config = (REPO / "deploy/nginx/catalyst-api.conf").read_text()
    assert "location ^~ /api/live-runs" in config
    assert "location = /api/data/update" in config
    assert "location = /api/data/refresh" in config
    assert "TasksMax=256" in (REPO / "deploy/api/catalyst-api.service").read_text()
    assert config.count("limit_req zone=catalyst_run") == 3
    assert "location /api/run" not in config


def test_generator_failure_restores_api_deploy_backup(tmp_path):
    import os

    root = tmp_path / "root"
    unit = root / "etc/systemd/system/catalyst-api.service"
    nginx = root / "etc/nginx/conf.d/catalyst-api.conf"
    allow = root / "etc/nginx/catalyst-cloudflare-allow.conf"
    for path, value in ((unit, "old unit\n"), (nginx, "old nginx\n"), (allow, "old allow\n")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    for name, body in (("python3", "exit 17"), ("nginx", "exit 0"), ("systemctl", "exit 0")):
        executable = fakebin / name
        executable.write_text("#!/bin/sh\n" + body + "\n")
        executable.chmod(0o755)
    fake_install = fakebin / "install"
    fake_install.write_text("#!/bin/sh\n[ \"$1\" = -D ] || exit 2\nmkdir -p \"$(dirname \"$3\")\"\ncp -a \"$2\" \"$3\"\n")
    fake_install.chmod(0o755)
    env = dict(os.environ, PATH=f"{fakebin}:/usr/bin:/bin")
    result = subprocess.run(["bash", str(REPO / "deploy/deploy_api.sh"), "--root", str(root)],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 17
    assert unit.read_text() == "old unit\n"
    assert nginx.read_text() == "old nginx\n"
    assert allow.read_text() == "old allow\n"
