from pathlib import Path


CONFIG = Path(__file__).resolve().parents[2] / "nginx" / "catalyst-api.conf"
DEPLOY_API = Path(__file__).resolve().parents[2] / "deploy_api.sh"


def test_public_origin_proxy_and_static_routes_are_configured() -> None:
    config = CONFIG.read_text()

    assert "listen 8443 ssl http2;" in config
    assert "server_name catalyst.yiannischen.dev;" in config
    assert "/etc/ssl/catalyst/origin.pem" in config
    assert "/etc/ssl/catalyst/origin.key" in config
    assert "root /opt/catalyst/web;" in config
    assert "try_files $uri $uri/ /index.html;" in config
    assert "no-cache, no-store, must-revalidate" in config
    assert "max-age=31536000, immutable" in config
    assert "proxy_pass http://127.0.0.1:8000;" in config
    assert "proxy_buffering off;" in config
    assert "proxy_read_timeout 300s;" in config
    assert "client_max_body_size 1m;" in config


def test_run_rate_limit_is_method_and_route_specific() -> None:
    config = CONFIG.read_text()

    assert '~^POST:/api/live-runs(?:/[^/]+/cancel)?$ $binary_remote_addr;' in config
    assert "limit_req zone=catalyst_run burst=3 nodelay;" in config
    assert "limit_req_zone $catalyst_run_rate_key zone=catalyst_run:10m rate=2r/m;" in config


def test_cloudflare_peer_is_rejected_and_forwarded_headers_are_explicit() -> None:
    config = CONFIG.read_text()

    assert "if ($catalyst_cloudflare_peer = 0) { return 444; }" in config
    assert "real_ip_header CF-Connecting-IP;" in config
    assert "proxy_set_header CF-Connecting-IP $remote_addr;" in config
    assert "proxy_set_header X-Forwarded-Proto https;" in config
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in config
    assert "proxy_set_header Host $host;" in config


def test_api_deploy_installs_http_context_include_and_reloads_nginx() -> None:
    deploy_script = DEPLOY_API.read_text()

    assert 'ALLOW="$ROOT/etc/nginx/conf.d/catalyst-cloudflare-allow.conf"' in deploy_script
    assert deploy_script.count("systemctl reload nginx") >= 3
