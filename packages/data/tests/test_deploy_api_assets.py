import importlib.util
import ipaddress
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
    source = REPO / "deploy/nginx/cloudflare_ips.txt"
    ranges = [str(ipaddress.ip_network(line.strip(), strict=False))
              for line in source.read_text().splitlines()
              if line.strip() and not line.lstrip().startswith("#")]
    module.generate(source, output)
    rendered = output.read_text()
    assert "geo $realip_remote_addr $catalyst_cloudflare_peer {" in rendered
    geo_block = rendered.split("geo $realip_remote_addr $catalyst_cloudflare_peer {", 1)[1].split("}", 1)[0]
    assert "default 0;" in geo_block
    for network in ranges:
        assert f"set_real_ip_from {network};" in rendered
        assert f"{network} 1;" in geo_block
    assert "allow " not in rendered
    assert "deny all;" not in rendered


def test_nginx_strict_limits_cover_live_and_data_write_routes():
    config = (REPO / "deploy/nginx/catalyst-api.conf").read_text()
    assert "if ($catalyst_cloudflare_peer = 0) { return 444; }" in config
    assert "limit_req_zone $catalyst_run_rate_key zone=catalyst_run:10m rate=2r/m;" in config
    assert 'map "$request_method:$uri" $catalyst_run_rate_key {' in config
    assert "    default \"\";" in config
    assert "    ~^POST:/api/live-runs(?:/[^/]+/cancel)?$ $binary_remote_addr;" in config
    assert "    ~^POST:/api/data/refresh$ $binary_remote_addr;" in config

    create_location = config.split("location = /api/live-runs {", 1)[1].split("\n    }", 1)[0]
    cancel_location = config.split("location ~ ^/api/live-runs/[^/]+/cancel$ {", 1)[1].split("\n    }", 1)[0]
    assert "limit_req zone=catalyst_run burst=3 nodelay;" in create_location
    refresh_location = config.split("location = /api/data/refresh {", 1)[1].split("\n    }", 1)[0]
    assert "limit_req zone=catalyst_run burst=3 nodelay;" in cancel_location
    assert "limit_req zone=catalyst_run burst=3 nodelay;" in refresh_location
    assert config.count("limit_req zone=catalyst_run") == 3

    # The map's empty default leaves GET history, view, and stream requests out
    # of the strict zone, including GET /api/live-runs's exact-match location.
    import re

    strict_key = re.compile(r"^POST:/api/live-runs(?:/[^/]+/cancel)?$")
    read_routes = (
        "GET:/api/live-runs",
        "GET:/api/live-runs/run-123/view",
        "GET:/api/live-runs/run-123/stream",
    )
    assert all(strict_key.match(route) is None for route in read_routes)


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
