"""A polite, single-site crawler that goes straight for contact-rich pages."""

from __future__ import annotations

import heapq
import logging
import re
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
                 user_agent: str = USER_AGENT, region: str = "US"):
        self.max_pages = max_pages
        self.delay = delay
        self.timeout = timeout
        self.region = region
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "text/html,*/*;q=0.5"})
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}

    def _robots_for(self, base: str) -> RobotFileParser | None:
        if base not in self._robots:
            rp = RobotFileParser()
            try:
                r = self.session.get(base + "/robots.txt", timeout=self.timeout)
                if r.status_code in (401, 403):
                    rp.disallow_all = True
                elif r.ok:
                    rp.parse(r.text.splitlines())
                else:
                    rp = None  # no robots.txt: everything allowed
            except requests.RequestException:
                rp = None
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
        if not self.allowed(url):
            log.info("robots.txt disallows %s", url)
            return None
        self._throttle(urlparse(url).netloc)
        try:
            try:
                r = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            except requests.exceptions.SSLError:
                # Some small-company sites have a broken certificate but load fine over http.
                if not url.startswith("https://"):
                    raise
                r = self.session.get("http://" + url[len("https://"):], timeout=self.timeout, allow_redirects=True)
        except requests.RequestException as e:
            log.info("fetch failed %s: %s", url, e)
            return None
        if not r.ok or "html" not in r.headers.get("Content-Type", ""):
            return None
        return r.url, r.text

    def sitemap_urls(self, base: str, limit: int = 2000) -> list[str]:
        rp = self._robots_for(base)
        maps = list((rp.site_maps() if rp else None) or []) or [base + "/sitemap.xml"]
        urls: list[str] = []
        fetched = 0
        while maps and fetched < 6 and len(urls) < limit:
            sm = maps.pop(0)
            fetched += 1
            if not self.allowed(sm):
                continue
            try:
                r = self.session.get(sm, timeout=self.timeout)
            except requests.RequestException:
                continue
            if not r.ok or "<loc>" not in r.text:
                continue
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)
            if "<sitemapindex" in r.text:
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
        """
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
        if use_sitemap:
            for url in self.sitemap_urls(base):
                if score(url) > 0 or (scorer and scorer(url, "") > 0):
                    push(url)

        fetched = 0
        while queue and fetched < self.max_pages:
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
