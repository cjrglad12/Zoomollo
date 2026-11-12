"""A polite, single-site crawler that goes straight for contact-rich pages."""

from __future__ import annotations

import heapq
import logging
import os
import re
import threading
import time
from typing import Iterator
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

from .extract import PageResult, extract_page

log = logging.getLogger(__name__)

USER_AGENT = "ZoomolloBot/0.1 (+contact discovery; respects robots.txt)"

SEED_PATHS = [
    "/", "/contact", "/contact-us", "/about", "/about-us", "/team", "/our-team",
    "/leadership", "/people", "/staff", "/directory", "/careers", "/jobs",
    "/company", "/management", "/locations", "/offices",
]

# Higher score = crawled sooner.
KEYWORDS = {
    "contact": 10, "team": 9, "staff": 9, "people": 8, "leadership": 8,
    "directory": 8, "about": 7, "management": 7, "bio": 6, "careers": 6,
    "jobs": 5, "office": 5, "location": 5, "press": 4, "company": 4,
}

SKIP_EXT = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".zip", ".mp4",
    ".mp3", ".mov", ".css", ".js", ".ico", ".xml", ".json", ".doc", ".docx",
    ".xls", ".xlsx", ".ppt", ".pptx", ".woff", ".woff2", ".ttf",
)

# A homepage or sitemap bigger than this is cut off. Parsing tens of megabytes
# holds the whole program (including the web UI) until it finishes.
MAX_BODY = 3_000_000


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def crawl_timeout() -> float:
    """Seconds one HTTP request may take. CRAWL_TIMEOUT in settings; default 15."""
    return _env_float("CRAWL_TIMEOUT", 15.0)


def crawl_budget() -> float:
    """Seconds one company crawl may take. CRAWL_BUDGET_SECONDS in settings; default 180."""
    return _env_float("CRAWL_BUDGET_SECONDS", 180.0)


def normalize_domain(domain_or_url: str) -> str:
    s = domain_or_url.strip()
    if "://" not in s:
        s = "https://" + s
    u = urlparse(s)
    host = u.hostname or u.netloc.rsplit("@", 1)[-1].split(":", 1)[0]  # drop ":443", logins
    return host.lower().rstrip(".").removeprefix("www.")


def score(url: str) -> int:
    path = urlparse(url).path.lower()
    s = sum(w for k, w in KEYWORDS.items() if k in path)
    return s - path.count("/")  # prefer shallow pages


class Crawler:
    def __init__(self, max_pages: int = 40, delay: float = 1.0, timeout: float = 15.0,
                 user_agent: str = USER_AGENT, region: str = "US", *, budget: float | None = None):
        self.max_pages = max_pages
        self.delay = delay
        self.timeout = timeout
        # Seconds for this crawler's whole life. crawl_company makes one crawler per company and
        # passes CRAWL_BUDGET_SECONDS. None (a crawler shared by a directory read or a prospect
        # search, across many sites) has no total limit; each request still has `timeout`.
        self.budget = budget
        self.region = region
        self.user_agent = user_agent
        self.deadline: float | None = None
        self.gave_up = False
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "text/html,*/*;q=0.5"})
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}

    def _ensure_deadline(self) -> None:
        if self.deadline is None:
            self.deadline = time.monotonic() + (self.budget if self.budget is not None else float("inf"))

    def _over(self) -> bool:
        """True once this company's time budget is spent."""
        self._ensure_deadline()
        if time.monotonic() >= self.deadline:
            self.gave_up = True
            return True
        return False

    def _reset_session(self) -> None:
        """Drop a request that ignored its timeout (a hung DNS lookup, a trickle). Closing the
        socket lets that attempt end; the next request uses a fresh session."""
        old = self.session
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent, "Accept": "text/html,*/*;q=0.5"})
        try:
            old.close()
        except Exception:  # noqa: BLE001
            pass

    def _read_capped(self, response, deadline: float) -> str | None:
        data = bytearray()
        try:
            for chunk in response.iter_content(65536):
                if time.monotonic() > deadline:
                    return None
                if not chunk:
                    continue
                data.extend(chunk)
                if len(data) >= MAX_BODY:
                    break  # keep the start of a huge page or sitemap; don't read the rest
        finally:
            response.close()
        encoding = response.encoding or "utf-8"
        return bytes(data).decode(encoding, errors="replace")

    def _request(self, url: str, allow_redirects: bool = True):
        """(response, text) or None. One request cannot outlive `timeout`, and nothing is read
        once the company budget is spent. The read runs on a side thread so a lookup that
        ignores socket timeouts (common on Windows) cannot stall the caller."""
        if self._over():
            return None
        req_deadline = min(self.deadline, time.monotonic() + self.timeout)
        wait = req_deadline - time.monotonic()
        if wait <= 0:
            self.gave_up = True
            return None
        box: dict = {}

        def run() -> None:
            try:
                sock = (min(5.0, self.timeout), min(self.timeout, max(0.1, wait)))
                try:
                    resp = self.session.get(url, timeout=sock, allow_redirects=allow_redirects, stream=True)
                except requests.exceptions.SSLError:
                    # Some small-company sites have a broken certificate but load fine over http.
                    if not url.startswith("https://"):
                        raise
                    resp = self.session.get("http://" + url[len("https://"):], timeout=sock,
                                            allow_redirects=allow_redirects, stream=True)
                box["resp"] = resp
                box["text"] = self._read_capped(resp, req_deadline)
            except Exception as exc:  # noqa: BLE001 - a failed fetch is a skip, not a crash
                box["err"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(wait)
        if thread.is_alive():
            self._reset_session()
            return None
        text = box.get("text")
        if box.get("err") is not None or text is None or "resp" not in box:
            return None
        return box["resp"], text

    def _robots_for(self, base: str) -> RobotFileParser | None:
        if base not in self._robots:
            rp = RobotFileParser()
            got = self._request(base + "/robots.txt")
            if not got:
                rp = None
            else:
                response, text = got
                if response.status_code in (401, 403):
                    rp.disallow_all = True
                elif response.ok:
                    rp.parse(text.splitlines())
                else:
                    rp = None  # no robots.txt: everything allowed
            self._robots[base] = rp
        return self._robots[base]

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        rp = self._robots_for(f"{p.scheme}://{p.netloc}")
        return rp is None or rp.can_fetch(self.session.headers["User-Agent"], url)

    def _throttle(self, host: str) -> None:
        wait = self.delay - (time.monotonic() - self._last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def fetch(self, url: str) -> tuple[str, str] | None:
        """Returns (final_url, html) or None."""
        if self._over():
            return None
        if not self.allowed(url):
            log.info("robots.txt disallows %s", url)
            return None
        self._throttle(urlparse(url).netloc)
        if self._over():
            return None
        got = self._request(url)
        if not got:
            log.info("fetch failed %s", url)
            return None
        response, text = got
        if not response.ok or "html" not in response.headers.get("Content-Type", ""):
            return None
        return response.url, text

    def sitemap_urls(self, base: str, limit: int = 2000) -> list[str]:
        rp = self._robots_for(base)
        maps = list((rp.site_maps() if rp else None) or []) or [base + "/sitemap.xml"]
        urls: list[str] = []
        fetched = 0
        while maps and fetched < 6 and len(urls) < limit and not self._over():
            sm = maps.pop(0)
            fetched += 1
            if not self.allowed(sm):
                continue
            got = self._request(sm)
            if not got:
                continue
            response, text = got
            if not response.ok or "<loc>" not in text:
                continue
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text)
            if "<sitemapindex" in text:
                # Child sitemaps: people / team / job ones first.
                maps += sorted(locs, key=lambda u: -score(u))
            else:
                urls += locs
        return urls[:limit]

    def crawl(self, domain_or_url: str, seed_paths: list[str] | None = None,
              scorer=None, include_subdomains: bool = False, keep_query: bool = False,
              use_sitemap: bool = True) -> Iterator[PageResult]:
        """Crawl one site, best pages first.

        scorer(url, link_text) -> extra priority, for custom crawls such as
        member directories. include_subdomains also follows e.g.
        business.chamber.org from chamber.org. keep_query keeps ?page=2 style links.
        Stops when the company time budget is spent, even if pages remain.
        """
        self._ensure_deadline()
        domain = normalize_domain(domain_or_url)
        base = f"https://{domain}"
        queue: list[tuple[int, int, str]] = []
        seen: set[str] = set()
        counter = 0

        def same_site(host: str) -> bool:
            return host == domain or (include_subdomains and host.endswith("." + domain))

        def push(url: str, text: str = "") -> None:
            nonlocal counter
            url = url.split("#")[0]
            if not keep_query:
                url = url.split("?")[0]
            url = url.rstrip("/") or url
            if url in seen or url.lower().split("?")[0].endswith(SKIP_EXT):
                return
            host = urlparse(url).netloc.lower().removeprefix("www.")
            if not same_site(host):
                return
            seen.add(url)
            counter += 1
            pri = score(url) + (scorer(url, text) if scorer else 0)
            heapq.heappush(queue, (-pri, counter, url))

        for path in (SEED_PATHS if seed_paths is None else seed_paths):
            push(base + path)
        # Sitemaps list pages the menus don't link to (bios, individual job posts).
        if use_sitemap and not self._over():
            for url in self.sitemap_urls(base):
                if score(url) > 0 or (scorer and scorer(url, "") > 0):
                    push(url)

        fetched = 0
        while queue and fetched < self.max_pages and not self._over():
            _, _, url = heapq.heappop(queue)
            got = self.fetch(url)
            if not got:
                continue
            final_url, html = got
            # Follow a redirect to www. or another canonical host once.
            final_host = urlparse(final_url).netloc.lower().removeprefix("www.")
            if final_host != domain and fetched == 0:
                domain = final_host
            fetched += 1
            page = extract_page(html, final_url, self.region)
            yield page
            for link in page.links:
                push(link, page.link_text.get(link, ""))
