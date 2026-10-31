"""Web search providers. Configure one with an environment variable:

  BRAVE_API_KEY   Brave Search API (has a free tier)   https://brave.com/search/api/
  SERPAPI_KEY     SerpAPI (Google results, paid)        https://serpapi.com/
  SEARXNG_URL     Your own SearXNG instance (free, self-hosted, JSON format enabled)

Without one, discovery falls back to OpenStreetMap only and people come only
from company websites.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import requests

from .crawler import USER_AGENT

log = logging.getLogger(__name__)


@dataclass
class Result:
    title: str
    url: str
    snippet: str = ""


class SearchProvider:
    name = "base"
    min_interval = 1.0  # seconds between calls

    def __init__(self):
        self._last = 0.0
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.calls = 0

    def _wait(self):
        gap = self.min_interval - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()

    def search(self, query: str, count: int = 20) -> list[Result]:
        self._wait()
        self.calls += 1
        try:
            return self._search(query, count)
        except requests.RequestException as e:
            log.warning("search failed (%s): %s", self.name, e)
            return []

    def _search(self, query: str, count: int) -> list[Result]:
        raise NotImplementedError


class Brave(SearchProvider):
    name = "brave"

    def __init__(self, key: str):
        super().__init__()
        self.session.headers.update({"X-Subscription-Token": key, "Accept": "application/json"})

    def _search(self, query, count):
        r = self.session.get("https://api.search.brave.com/res/v1/web/search",
                             params={"q": query, "count": min(count, 20), "country": "us"}, timeout=20)
        r.raise_for_status()
        return [Result(x.get("title", ""), x.get("url", ""), x.get("description", ""))
                for x in (r.json().get("web") or {}).get("results", [])]


class SerpApi(SearchProvider):
    name = "serpapi"

    def __init__(self, key: str):
        super().__init__()
        self.key = key

    def _search(self, query, count):
        r = self.session.get("https://serpapi.com/search.json", timeout=30,
                             params={"q": query, "engine": "google", "num": count, "gl": "us",
                                     "api_key": self.key})
        r.raise_for_status()
        return [Result(x.get("title", ""), x.get("link", ""), x.get("snippet", ""))
                for x in r.json().get("organic_results", [])]


class Searxng(SearchProvider):
    name = "searxng"

    def __init__(self, url: str):
        super().__init__()
        self.url = url.rstrip("/")

    def _search(self, query, count):
        r = self.session.get(f"{self.url}/search", params={"q": query, "format": "json"}, timeout=30)
        r.raise_for_status()
        return [Result(x.get("title", ""), x.get("url", ""), x.get("content", ""))
                for x in r.json().get("results", [])[:count]]


def get_provider() -> SearchProvider | None:
    if os.environ.get("BRAVE_API_KEY"):
        return Brave(os.environ["BRAVE_API_KEY"])
    if os.environ.get("SERPAPI_KEY"):
        return SerpApi(os.environ["SERPAPI_KEY"])
    if os.environ.get("SEARXNG_URL"):
        return Searxng(os.environ["SEARXNG_URL"])
    return None
