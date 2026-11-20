"""Fetch relay for hosts that do not serve the update server's client.

Runs on a machine those hosts serve (the operator's Mac), bound to localhost,
reached from the update server through an SSH reverse tunnel. It fetches with
the same Fetcher as everywhere else: same User-Agent, robots.txt per hop, one
request per second per host. Only the hosts in RELAY_HOSTS are relayed, so it
is never a general proxy.

  # on the Mac
  python3 -m catalyst_data.ingestion.collectors.relay --port 8765
  ssh -N -R 127.0.0.1:8765:127.0.0.1:8765 "$CATALYST_DEPLOY_HOST"
  # on the server
  CATALYST_RELAY_URL=http://127.0.0.1:8765 CATALYST_RELAY_HOSTS=www.benzinga.com
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests

from catalyst_data.ingestion.collectors import fetch

RELAY_HOSTS = {"www.benzinga.com", "benzinga.com"}

# A Benzinga article page is about 1.1 MB, almost all of it scripts; the reverse
# tunnel moves about 10 KB/s. The server reads only the JSON-LD metadata and the
# article markup, and its extractor drops script/style/noscript anyway, so those
# are removed here (JSON-LD kept) and the response is gzipped.
_DROP = re.compile(
    r"<script(?![^>]*application/ld\+json)[^>]*>.*?</script\s*>"
    r"|<style[^>]*>.*?</style\s*>|<noscript[^>]*>.*?</noscript\s*>|<svg[^>]*>.*?</svg\s*>|<!--.*?-->",
    re.IGNORECASE | re.DOTALL,
)


def slim_html(page_html: str) -> str:
    return _DROP.sub("", page_html)


# A per-item Fetcher marks a host down for its short life; the relay lives for days,
# so a down mark expires and the host is tried again.
DOWN_TTL_S = 120


def make_handler(fetcher: fetch.Fetcher, log=print, clock=time.monotonic):
    marked: dict[str, float] = {}

    def expire_down_marks() -> None:
        now = clock()
        for host in list(getattr(fetcher, "down", {})):
            first = marked.setdefault(host, now)
            if now - first >= DOWN_TTL_S:
                fetcher.down.pop(host, None)
                marked.pop(host, None)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict) -> None:
            body = gzip.compress(json.dumps(payload).encode(), compresslevel=6)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            url = (parse_qs(parsed.query).get("url") or [""])[0]
            if urlparse(url).netloc not in RELAY_HOSTS:
                self._send(403, {"error": "host not relayed"})
                return
            expire_down_marks()
            try:
                if parsed.path == "/allowed":
                    self._send(200, {"allowed": fetcher.allowed(url)})
                elif parsed.path == "/get":
                    resp = fetcher.get(url)
                    self._send(200, {"status": resp.status_code, "url": resp.url,
                                     "headers": {"content-type": resp.headers.get("content-type", "")},
                                     "text": slim_html(resp.text) if resp.status_code == 200 else ""})
                else:
                    self._send(404, {"error": "unknown action"})
            except fetch.HostUnavailable as exc:
                # Answer instead of dropping the connection: the server defers or retries.
                self._send(503, {"error": str(exc)})
            except requests.RequestException as exc:
                self._send(502, {"error": f"{type(exc).__name__}"})

        def log_message(self, fmt, *args) -> None:
            log(f"relay {self.address_string()} {fmt % args}")

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(fetch.Fetcher()))
    print(f"relay on 127.0.0.1:{args.port} for {sorted(RELAY_HOSTS)}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
