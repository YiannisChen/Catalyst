"""Public web page ingestion helpers for M9."""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from collections import defaultdict
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

USER_AGENT = "CatalystResearchBot/0.1 (student research; attribution study)"
MIN_BODY_CHARS = 600
PER_HOST_INTERVAL = 1.0
SOURCE_CLASS_BY_HOST = {
    "www.globenewswire.com": "corporate_press_release",
    "www.prnewswire.com": "corporate_press_release",
    "www.businesswire.com": "corporate_press_release",
    "www.fool.com": "analysis_opinion",
}
DEFAULT_SOURCE_CLASS = "reported_news"


# Per-host pacing is process-wide: successive refreshes (each with its own
# Fetcher) must not together exceed one request per second per host.
_NEXT_SLOT: dict[str, float] = defaultdict(float)
_SLOT_LOCK = threading.Lock()
MAX_REDIRECTS = 5
# Relay: hosts listed in CATALYST_RELAY_HOSTS are fetched by a relay process
# (catalyst_data.ingestion.collectors.relay) at CATALYST_RELAY_URL, on a machine whose client those
# hosts serve; the relay applies the same robots.txt and pacing rules.
RELAY_TIMEOUT_S = 120


class HostUnavailable(RuntimeError):
    """A host did not answer (robots.txt or page): the URL is deferred, never treated
    as disallowed or empty. Subclasses never inherit from requests.RequestException,
    so fetch_page lets them through to the caller."""

    def __init__(self, host: str, reason: str):
        super().__init__(f"{host} unavailable: {reason}")
        self.host = host


class RelayUnavailable(HostUnavailable):
    """The relay did not answer: the step fails (and is retried), it is never an empty page."""


RELAY_PROBE_TIMEOUT_S = 5


def relay_status(env: dict | None = None, *, get=requests.get) -> dict:
    """Ask the relay whether it is there, without fetching anything.

    A live relay answers a request with no url by refusing it (403 "host not relayed"), so any non-5xx answer means the
    tunnel and the process are up; a connection error, timeout or 5xx means the relay is unavailable. Run before the
    daily update so an outage is named in the log and the report instead of surfacing as slow, failing page fetches.
    """
    env = os.environ if env is None else env
    url = env.get("CATALYST_RELAY_URL", "").rstrip("/")
    if not url:
        return {"configured": False, "up": None}
    try:
        answer = get(f"{url}/allowed", timeout=RELAY_PROBE_TIMEOUT_S)
    except requests.RequestException as exc:
        return {"configured": True, "up": False, "reason": type(exc).__name__}
    if getattr(answer, "status_code", 200) >= 500:  # a proxy in front of a dead tunnel answers 502/504
        return {"configured": True, "up": False, "reason": f"HTTP {answer.status_code}"}
    return {"configured": True, "up": True}


class TransientPageError(RuntimeError):
    """A page response that may succeed on a later cache-fill attempt."""


class PermanentPageError(RuntimeError):
    """A page outcome that should be cached as a final miss."""


def prefer_ipv4() -> None:
    """Resolve IPv4 only when CATALYST_PREFER_IPV4=1 (hosts with broken IPv6 routes)."""
    if os.environ.get("CATALYST_PREFER_IPV4") == "1":
        import urllib3.util.connection as connection

        connection.allowed_gai_family = lambda: socket.AF_INET


class _RelayResponse:
    def __init__(self, payload: dict):
        self.status_code = int(payload["status"])
        self.url = payload["url"]
        self.headers = requests.structures.CaseInsensitiveDict(payload.get("headers") or {})
        self.text = payload.get("text") or ""
        self.is_redirect = False


class Fetcher:
    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session if session is not None else requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.robots: dict[str, robotparser.RobotFileParser | None] = {}
        self.next_slot = _NEXT_SLOT
        self.lock = _SLOT_LOCK
        self.robots_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self.down: dict[str, str] = {}  # hosts that did not answer during this Fetcher's life
        self.relay_url = os.environ.get("CATALYST_RELAY_URL", "").rstrip("/")
        self.relay_hosts = {
            h.strip() for h in os.environ.get("CATALYST_RELAY_HOSTS", "").split(",") if h.strip()
        } if self.relay_url else set()
        prefer_ipv4()

    def _relay(self, url: str) -> bool:
        return urlparse(url).netloc in self.relay_hosts

    def _call_relay(self, action: str, url: str) -> dict:
        try:
            resp = requests.get(f"{self.relay_url}/{action}", params={"url": url}, timeout=RELAY_TIMEOUT_S)
        except requests.RequestException as exc:
            raise RelayUnavailable(urlparse(url).netloc, f"relay unreachable: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise RelayUnavailable(urlparse(url).netloc, f"relay error {resp.status_code}: {resp.text[:200]}")
        return json.loads(resp.text)

    def _wait(self, host: str) -> None:
        # Reserve the host's next 1-second slot under the lock, sleep outside it.
        with self.lock:
            slot = max(time.monotonic(), self.next_slot[host])
            self.next_slot[host] = slot + PER_HOST_INTERVAL
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def allowed(self, url: str) -> bool:
        if self._relay(url):
            return bool(self._call_relay("allowed", url)["allowed"])
        parts = urlparse(url)
        host = parts.netloc
        with self.robots_locks[host]:
            self._load_robots(parts, host)
        parser = self.robots[host]
        return parser is not None and parser.can_fetch(USER_AGENT, url)

    def _load_robots(self, parts, host: str) -> None:
        if host in self.down:
            raise HostUnavailable(host, self.down[host])
        if host not in self.robots:
            parser = robotparser.RobotFileParser()
            try:
                self._wait(host)
                resp = self.session.get(f"{parts.scheme}://{host}/robots.txt", timeout=15)
                if resp.status_code >= 400:
                    parser = None if resp.status_code in (401, 403) else parser
                    if parser is not None:
                        parser.parse([])
                else:
                    parser.parse(resp.text.splitlines())
            except requests.RequestException as exc:
                # No answer is not a refusal: defer the host's URLs instead of skipping them.
                self.down[host] = f"robots.txt {type(exc).__name__}"
                raise HostUnavailable(host, self.down[host]) from exc
            self.robots[host] = parser

    def get(self, url: str) -> requests.Response:
        """GET with backoff on 429/5xx (Finnhub's redirect host throttles
        bursts; one 429 used to drop the whole article, as for h44)."""
        if self._relay(url):
            return _RelayResponse(self._call_relay("get", url))
        resp = None
        host = urlparse(url).netloc
        if host in self.down:
            raise HostUnavailable(host, self.down[host])
        for attempt in range(3):
            try:
                resp = self._get_following(url)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == 2:
                    self.down[host] = f"page {type(exc).__name__}"
                    raise HostUnavailable(host, self.down[host]) from exc
                time.sleep(5 * (attempt + 1))
                continue
            if resp.status_code != 429 and resp.status_code < 500:
                return resp
            time.sleep(5 * (attempt + 1))
        return resp

    def _get_following(self, url: str) -> requests.Response:
        """Follow redirects by hand: every hop is robots-checked and paced
        before it is requested. A disallowed hop returns its redirect response
        (not 200), which callers treat as unavailable."""
        resp = None
        for _ in range(MAX_REDIRECTS + 1):
            self._wait(urlparse(url).netloc)
            resp = self.session.get(url, timeout=20, allow_redirects=False)
            location = resp.headers.get("location")
            if not resp.is_redirect or not location:
                return resp
            url = requests.compat.urljoin(url, location)
            if not self.allowed(url):
                return resp
        return resp

    def fetch_page(self, url: str) -> tuple[str, str] | None:
        try:
            if not self.allowed(url):
                return None
            response = self.get(url)
            if response.status_code != 200 or not self.allowed(response.url):
                return None
            if "text/html" not in response.headers.get("content-type", ""):
                return None
            return response.url, response.text
        except requests.RequestException:
            return None

    def fetch_page_detailed(self, url: str) -> tuple[str, str] | None:
        """Fetch a page while preserving permanent versus transient miss details.

        Existing body-ingestion callers keep using ``fetch_page`` and its historical
        nullable result. Headline media caching needs to distinguish a final 404 or
        robots refusal from an outage that should be retried on a later run.
        """
        if not self.allowed(url):
            raise PermanentPageError("robots_disallowed")
        response = self.get(url)
        status = response.status_code
        if status == 429 or status >= 500:
            raise TransientPageError(f"http_{status}")
        if status != 200:
            raise PermanentPageError(f"http_{status}")
        if not self.allowed(response.url):
            raise PermanentPageError("redirect_robots_disallowed")
        if "text/html" not in response.headers.get("content-type", ""):
            raise PermanentPageError("not_html")
        return response.url, response.text


# Pages are fetched by several threads at once; politeness is unchanged because every request still reserves its
# host's next slot (one request per second per host, shared by all threads and Fetchers in the process).
FETCH_WORKERS = int(os.environ.get("CATALYST_FETCH_WORKERS", "8"))


class PrefetchedFetcher:
    """A Fetcher whose ``fetch_page`` answers from pages fetched in parallel beforehand.

    The caller then processes items in its own order exactly as before; only the network waits overlap. A URL not
    prefetched, or any other attribute, goes to the wrapped Fetcher. An exception raised while prefetching a URL
    (e.g. HostUnavailable) is raised again when that URL is asked for, so callers' error handling is unchanged.
    """

    def __init__(self, fetcher, urls, workers: int | None = None) -> None:
        from concurrent.futures import ThreadPoolExecutor

        self._fetcher = fetcher
        self._pages: dict[str, object] = {}
        unique = list(dict.fromkeys(u for u in urls if u))
        workers = FETCH_WORKERS if workers is None else workers
        if workers <= 1 or len(unique) <= 1:
            return  # nothing to overlap: fetch lazily, as before

        def one(url):
            try:
                return url, fetcher.fetch_page(url)
            except Exception as exc:  # noqa: BLE001  (re-raised on use)
                return url, exc
        with ThreadPoolExecutor(max_workers=min(workers, len(unique))) as pool:
            for url, result in pool.map(one, unique):
                self._pages[url] = result

    def fetch_page(self, url):
        if url not in self._pages:
            return self._fetcher.fetch_page(url)
        result = self._pages.pop(url)
        if isinstance(result, Exception):
            raise result
        return result

    def __getattr__(self, name):
        return getattr(self._fetcher, name)


def strip_zero_width(text: str) -> str:
    return text.translate({ord(char): None for char in "\u200b\u200c\u200d\u2060\ufeff"})


def extract(page_html: str, title: str) -> str | None:
    soup = BeautifulSoup(page_html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
        tag.decompose()
    # The body is the largest <article>/<main>; small <article> cards are teasers.
    candidates = soup.find_all(["article", "main"])
    if not candidates:
        blocks = {}
        for p in soup.find_all("p"):
            blocks.setdefault(p.parent, []).append(p)
        if not blocks:
            return None
        parent = max(blocks, key=lambda k: sum(len(x.get_text(" ", strip=True)) for x in blocks[k]))
        candidates = [parent]
    best = max(candidates, key=lambda c: len(c.get_text(" ", strip=True)))
    paragraphs = [
        " ".join(p.get_text(" ", strip=True).split())
        for p in best.find_all(["p", "li"])
    ]
    # Strip wire watermarks before any check, so quotes and title terms match.
    text = strip_zero_width("\n\n".join(p for p in paragraphs if len(p) > 30))
    if len(text) < MIN_BODY_CHARS:
        return None
    key_terms = {w for w in re.findall(r"[a-z]{5,}", title.lower())}
    if key_terms and not any(term in text.lower() for term in key_terms):
        return None
    return text


_IMAGE_META = (
    (("property", "og:image"), ("og",)),
    (("property", "og:image:secure_url"), ("og",)),
    (("name", "twitter:image"), ("twitter",)),
    (("property", "twitter:image"), ("twitter",)),
)


def page_image_with_source(page_html: str, page_url: str) -> tuple[str, str] | None:
    """Return a usable HTTPS article image and the metadata family it came from."""
    soup = BeautifulSoup(page_html, "html.parser")
    candidates: list[tuple[str, str]] = []
    for (attribute, value), (source,) in _IMAGE_META:
        tag = soup.find("meta", attrs={attribute: re.compile(rf"^{re.escape(value)}$", re.I)})
        if tag and tag.get("content"):
            candidates.append((str(tag["content"]).strip(), source))
    link = soup.find(
        "link",
        rel=lambda rel: rel and "image_src" in [
            x.lower() for x in (rel if isinstance(rel, list) else str(rel).split())
        ],
    )
    if link and link.get("href"):
        candidates.append((str(link["href"]).strip(), "og"))

    for raw_url, source in candidates:
        resolved = urljoin(page_url, raw_url)
        parsed = urlparse(resolved)
        path = parsed.path.lower()
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            continue
        if is_placeholder_image(resolved):
            continue
        return resolved, source
    return None


def is_placeholder_image(url: str) -> bool:
    lowered = url.lower()
    path = urlparse(url).path.lower()
    return (
        any(marker in lowered for marker in (
            "placeholder", "default-image", "default_image", "no-image", "no_image",
            "stock-photo-placeholder", "yahoo_finance",
        ))
        or "logo" in path or "favicon" in path or "sprite" in path
        or re.search(r"(?:^|[/_-])1x1(?:[/_.-]|$)", path) is not None
    )


def page_image(page_html: str, page_url: str) -> str | None:
    result = page_image_with_source(page_html, page_url)
    return result[0] if result else None
