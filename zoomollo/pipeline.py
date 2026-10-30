"""Glue: crawl / fetch jobs, then save everything to the database."""

from __future__ import annotations

import csv
import io
import logging
import re
from urllib.parse import urlparse
from typing import Callable

from .crawler import Crawler, normalize_domain
from .db import DB, now
from .extract import is_role_email, looks_like_name, social_kind
from .jobboards import fetch_jobs
from .patterns import guess_emails, has_mx, infer_pattern, split_name

log = logging.getLogger(__name__)
Progress = Callable[[str], None]


def _kind(email: str) -> str:
    return "role" if is_role_email(email) else "person"


def crawl_company(db: DB, domain: str, max_pages: int = 40, delay: float = 1.0,
                  region: str = "US", progress: Progress | None = None) -> dict:
    domain = normalize_domain(domain)
    company_id = db.company(domain, domain=domain)
    crawler = Crawler(max_pages=max_pages, delay=delay, region=region)
    found = {"pages": 0, "emails": 0, "phones": 0, "people": 0}

    for page in crawler.crawl(domain):
        found["pages"] += 1
        if progress:
            progress(f"{page.url}: {len(page.emails)} emails, {len(page.phones)} phones")
        if page.title and urlparse(page.url).path in ("", "/"):
            db.update_company(company_id, name=company_name_from_title(page.title))

        named = {p.email: p for p in page.people if p.email}
        for email in page.emails:
            p = named.get(email)
            name = p.name if p and looks_like_name(p.name) else None
            if db.add_contact(company_id, "email", email, _kind(email), name=name,
                              title=p.title if p else None, source_url=page.url):
                found["emails"] += 1
        for p in page.people:
            if p.phone and p.name:
                db.add_contact(company_id, "phone", p.phone, "person", name=p.name,
                               title=p.title, source_url=page.url)
        for phone in page.phones:
            if db.add_contact(company_id, "phone", phone, "company", source_url=page.url):
                found["phones"] += 1
        found["people"] += sum(1 for p in page.people if p.name)
        for url in page.socials:
            db.add_social(company_id, social_kind(url) or "other", url)
        db.commit()

    learn_pattern(db, company_id)
    db.update_company(company_id, last_crawled=now())
    db.commit()
    return found


def company_name_from_title(title: str) -> str:
    """'Acme Inc - Home' / 'Home | Acme' -> 'Acme Inc'."""
    parts = [p.strip() for p in re.split(r"\s+[|\-–—:]\s+", title) if p.strip()]
    parts = [p for p in parts if p.lower() not in {"home", "homepage", "welcome"}] or parts
    return min(parts, key=len)[:120] if parts else title[:120]


def learn_pattern(db: DB, company_id: int) -> str | None:
    result = infer_pattern(db.named_emails(company_id))
    if result:
        db.update_company(company_id, email_pattern=result[0], pattern_confidence=round(result[1], 2))
        return result[0]
    return None


def import_jobs(db: DB, board: str, slug: str, domain: str | None = None,
                region: str = "US") -> dict:
    jobs = fetch_jobs(board, slug, region)
    key = normalize_domain(domain) if domain else f"{board}:{slug}"
    company_id = db.company(key, domain=normalize_domain(domain) if domain else None, name=slug)
    emails = phones = 0
    for j in jobs:
        db.add_job(company_id, j.board, j.external_id, j.title, j.location, j.url, j.posted_at)
        for e in j.emails:
            emails += db.add_contact(company_id, "email", e, _kind(e), source_url=j.url)
        for p in j.phones:
            phones += db.add_contact(company_id, "phone", p, "company", source_url=j.url)
    db.commit()
    return {"jobs": len(jobs), "emails": emails, "phones": phones}


def guess_for_person(db: DB, full_name: str, domain: str, title: str | None = None,
                     save: bool = True) -> list[str]:
    """Best-guess emails for someone at a domain. Saves only the top guess."""
    domain = normalize_domain(domain)
    row = db.conn.execute("SELECT id, email_pattern FROM companies WHERE key = ?", (domain,)).fetchone()
    pattern = row["email_pattern"] if row else None
    guesses = guess_emails(full_name, domain, pattern)
    if save and guesses and split_name(full_name) and has_mx(domain) is not False:
        company_id = row["id"] if row else db.company(domain, domain=domain)
        db.add_contact(company_id, "email", guesses[0], "guessed", name=full_name, title=title)
        db.commit()
    return guesses


EXPORT_FIELDS = ["company", "domain", "name", "title", "type", "value", "kind", "source_url",
                 "first_seen", "last_seen"]


def export_csv(db: DB, q: str = "", type_: str | None = None, kind: str | None = None) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_FIELDS)
    for r in db.search(q, type_, kind, limit=1_000_000):
        w.writerow([r["company_name"] or r["company_key"], r["company_domain"], r["name"], r["title"],
                    r["type"], r["value"], r["kind"], r["source_url"], r["first_seen"], r["last_seen"]])
    return buf.getvalue()
