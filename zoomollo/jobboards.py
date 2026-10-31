"""Public job-board APIs (Greenhouse, Lever, Ashby, Workable).

These are the official, unauthenticated JSON feeds each ATS publishes for a
company's careers page, so no HTML scraping or ToS-breaking is involved.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field

import requests

from .crawler import USER_AGENT
from .extract import extract_text


@dataclass
class Job:
    board: str
    company_slug: str
    external_id: str
    title: str
    location: str | None
    url: str | None
    posted_at: str | None
    description: str = ""
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)


def _get(url: str, **params):
    r = requests.get(url, params=params, timeout=20, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return r.json()


def greenhouse(slug: str) -> list[Job]:
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", content="true")
    return [
        Job("greenhouse", slug, str(j["id"]), j.get("title", ""),
            (j.get("location") or {}).get("name"), j.get("absolute_url"),
            j.get("updated_at"), html.unescape(j.get("content") or ""))
        for j in data.get("jobs", [])
    ]


def lever(slug: str) -> list[Job]:
    data = _get(f"https://api.lever.co/v0/postings/{slug}", mode="json")
    jobs = []
    for j in data:
        parts = [j.get("descriptionPlain") or ""]
        for lst in j.get("lists") or []:
            parts.append(lst.get("text", "") + " " + (lst.get("content") or ""))
        parts.append(j.get("additionalPlain") or "")
        jobs.append(Job("lever", slug, j["id"], j.get("text", ""),
                        (j.get("categories") or {}).get("location"), j.get("hostedUrl"),
                        str(j.get("createdAt") or ""), "\n".join(parts)))
    return jobs


def ashby(slug: str) -> list[Job]:
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    return [
        Job("ashby", slug, j.get("id") or j.get("jobUrl", ""), j.get("title", ""),
            j.get("location"), j.get("jobUrl"), j.get("publishedAt"),
            j.get("descriptionHtml") or j.get("descriptionPlain") or "")
        for j in data.get("jobs", [])
    ]


def workable(slug: str) -> list[Job]:
    data = _get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", details="true")
    jobs = []
    for j in data.get("jobs", []):
        loc = ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x)
        jobs.append(Job("workable", slug, j.get("shortcode", ""), j.get("title", ""),
                        loc or None, j.get("url"), j.get("published_on"),
                        j.get("description", "")))
    return jobs


BOARDS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workable": workable}


def fetch_jobs(board: str, slug: str, region: str = "US") -> list[Job]:
    if board not in BOARDS:
        raise ValueError(f"Unknown board '{board}'. Use one of: {', '.join(BOARDS)}")
    jobs = BOARDS[board](slug)
    for j in jobs:
        j.emails, j.phones = extract_text(j.description, region)
    return jobs


def detect_board(slug: str) -> list[tuple[str, int]]:
    """Try every board for a slug; returns [(board, job_count)] that answered."""
    found = []
    for name, fn in BOARDS.items():
        try:
            n = len(fn(slug))
        except (requests.RequestException, ValueError):
            continue
        if n:
            found.append((name, n))
    return found
