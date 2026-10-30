"""Glue: discover companies, crawl them, pull jobs, map people, save it all."""

from __future__ import annotations

import csv
import io
import logging
import re
from typing import Callable
from urllib.parse import urlparse

import requests

from .crawler import Crawler, normalize_domain
from .db import DB, now
from .extract import is_role_email, looks_like_name, normalize_phone, social_kind
from .jobboards import fetch_jobs
from .orgmap import search_people
from .patterns import PATTERNS, has_mx, infer_pattern, render, split_name
from .search import SearchProvider

log = logging.getLogger(__name__)
Progress = Callable[[str], None]


def _kind(email: str) -> str:
    return "role" if is_role_email(email) else "person"


def company_name_from_title(title: str) -> str:
    """'Acme Inc - Home' / 'Home | Acme' -> 'Acme Inc'."""
    parts = [p.strip() for p in re.split(r"\s+[|\-–—:]\s+", title) if p.strip()]
    parts = [p for p in parts if p.lower() not in {"home", "homepage", "welcome"}] or parts
    return min(parts, key=len)[:120] if parts else title[:120]


# --- websites ---------------------------------------------------------------

def crawl_company(db: DB, domain: str, max_pages: int = 40, delay: float = 1.0,
                  region: str = "US", progress: Progress | None = None,
                  import_ats: bool = True) -> dict:
    domain = normalize_domain(domain)
    company_id = db.company(domain, domain=domain)
    crawler = Crawler(max_pages=max_pages, delay=delay, region=region)
    found = {"pages": 0, "emails": 0, "phones": 0, "people": 0, "jobs": 0}
    ats: set[tuple[str, str]] = set()

    for page in crawler.crawl(domain):
        found["pages"] += 1
        if progress:
            progress(f"{page.url}: {len(page.emails)} emails, {len(page.phones)} phones")
        if page.title and urlparse(page.url).path in ("", "/"):
            row = db.company_row(company_id)
            if not row["name"] or row["name"] == row["key"]:
                db.update_company(company_id, name=company_name_from_title(page.title))
        ats |= page.ats

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
            if p.name and looks_like_name(p.name):
                if db.add_person(company_id, p.name, p.title, email=p.email, phone=p.phone,
                                 source="website", source_url=page.url):
                    found["people"] += 1
        for phone in page.phones:
            if db.add_contact(company_id, "phone", phone, "company", source_url=page.url):
                found["phones"] += 1
        for url in page.socials:
            db.add_social(company_id, social_kind(url) or "other", url)
        for j in page.jobs:
            db.add_job(company_id, "website", j["external_id"], j["title"], j["location"], j["url"],
                       j["posted_at"], j["description"])
            found["jobs"] += 1
        db.commit()

    if import_ats:
        for board, slug in sorted(ats):
            try:
                found["jobs"] += import_jobs(db, board, slug, domain=domain)["jobs"]
            except (requests.RequestException, ValueError) as e:
                log.info("job board %s:%s failed: %s", board, slug, e)

    learn_pattern(db, company_id)
    db.update_company(company_id, last_crawled=now())
    db.commit()
    assign_hiring_managers(db, company_id)
    return found


def learn_pattern(db: DB, company_id: int) -> str | None:
    """Learn the email format from named emails, and from known people whose
    names line up with addresses found anywhere on the site."""
    examples = db.named_emails(company_id)
    row = db.company_row(company_id)
    domain = row["domain"] if row else None
    if domain:
        locals_ = {r[0].split("@")[0]: r[0] for r in db.conn.execute(
            "SELECT value FROM contacts WHERE company_id = ? AND type = 'email' AND value LIKE ?",
            (company_id, f"%@{domain}"))}
        for p in db.people(company_id):
            split = split_name(p["name"])
            if not split:
                continue
            for pat in PATTERNS:
                local = render(pat, *split)
                if local in locals_:
                    examples.append((p["name"], locals_[local]))
                    db.add_person(company_id, p["name"], email=locals_[local], email_status="found")
                    break
    result = infer_pattern(list(dict.fromkeys(examples)))
    if result:
        db.update_company(company_id, email_pattern=result[0], pattern_confidence=round(result[1], 2))
        return result[0]
    return None


# --- job boards -------------------------------------------------------------

def import_jobs(db: DB, board: str, slug: str, domain: str | None = None,
                region: str = "US", company_id: int | None = None) -> dict:
    jobs = fetch_jobs(board, slug, region)
    if company_id is None:
        key = normalize_domain(domain) if domain else f"{board}:{slug}"
        company_id = db.company(key, domain=normalize_domain(domain) if domain else None, name=slug)
    emails = phones = 0
    for j in jobs:
        db.add_job(company_id, j.board, j.external_id, j.title, j.location, j.url, j.posted_at, j.description)
        for e in j.emails:
            emails += db.add_contact(company_id, "email", e, _kind(e), source_url=j.url)
        for p in j.phones:
            phones += db.add_contact(company_id, "phone", p, "company", source_url=j.url)
    db.commit()
    return {"jobs": len(jobs), "emails": emails, "phones": phones}


# --- people -----------------------------------------------------------------

def map_people(db: DB, company_id: int, provider: SearchProvider, max_queries: int = 4) -> int:
    """Find people at a company through search-engine results for public profiles."""
    row = db.company_row(company_id)
    name = row["name"] or row["key"]
    depts = [r[0] for r in db.conn.execute(
        "SELECT department, COUNT(*) n FROM jobs WHERE company_id = ? GROUP BY department ORDER BY n DESC",
        (company_id,))]
    found = search_people(provider, name, row["city"], depts, max_queries=max_queries)
    for f in found:
        db.add_person(company_id, f.name, f.title, linkedin_url=f.url, source="search")
    db.commit()
    return len(found)


def assign_hiring_managers(db: DB, company_id: int) -> int:
    """Cross-reference people and contacts with the company's open roles."""
    from .xref import cross_reference
    return cross_reference(db, company_id)


def guess_people_emails(db: DB, company_id: int) -> int:
    """First best-guess email for each person without one (see emailcheck for the retry loop)."""
    from .emailcheck import guess_all
    row = db.company_row(company_id)
    if not row or not row["domain"] or has_mx(row["domain"]) is False:
        return 0
    return guess_all(db, company_id)


def guess_for_person(db: DB, full_name: str, domain: str, title: str | None = None,
                     save: bool = True) -> list[str]:
    """Ordered email guesses for someone at a domain. With save, the person is
    added and given the top guess, which then joins the bounce/retry loop."""
    from .emailcheck import next_guess, ranked_patterns
    from .patterns import candidates
    domain = normalize_domain(domain)
    row = db.conn.execute("SELECT id FROM companies WHERE key = ?", (domain,)).fetchone()
    preferred, avoid = ranked_patterns(db, row["id"]) if row else ([], set())
    guesses = [e for e, _ in candidates(full_name, domain, preferred, avoid) if not db.is_bad_email(e)]
    if save and guesses and split_name(full_name) and has_mx(domain) is not False:
        company_id = row["id"] if row else db.company(domain, domain=domain)
        pid = db.add_person(company_id, full_name, title, source="manual")
        if pid and not db.conn.execute("SELECT email FROM people WHERE id = ?", (pid,)).fetchone()[0]:
            next_guess(db, pid)
        db.commit()
    return guesses


# --- the whole thing ----------------------------------------------------------

def prospect(db: DB, what: str, where: str, limit: int = 25, crawl_pages: int = 15,
             provider: SearchProvider | None = None, find_people: bool = True,
             use_osm: bool = True, progress: Progress | None = None) -> dict:
    """'Commercial HVAC' + 'Austin, TX' -> companies, jobs, people, hiring managers, emails."""
    from .discover import find_companies

    say = progress or (lambda m: None)
    say(f"Looking for {what} in {where}…")
    cands = find_companies(what, where, limit, provider, use_osm=use_osm, progress=say)
    say(f"Found {len(cands)} companies")
    totals = {"companies": 0, "people": 0, "hiring_managers": 0, "jobs": 0, "emails": 0, "phones": 0}

    for i, c in enumerate(cands, 1):
        cid = db.company(c.key, domain=c.domain, name=c.name)
        db.update_company(cid, city=where, industry=what, source=",".join(sorted(c.sources)) or None,
                          **{k: v for k, v in {"address": c.address, "phone": c.phone}.items() if v})
        if c.phone and (e164 := normalize_phone(c.phone)):
            db.add_contact(cid, "phone", e164, "company", source_url="openstreetmap")
        if c.email:
            db.add_contact(cid, "email", c.email, _kind(c.email), source_url="openstreetmap")
        db.commit()
        totals["companies"] += 1
        label = f"[{i}/{len(cands)}] {c.name}" + (f" ({c.domain})" if c.domain else "")

        if c.domain:
            try:
                r = crawl_company(db, c.domain, max_pages=crawl_pages, delay=1.0)
                totals["emails"] += r["emails"]
                totals["phones"] += r["phones"]
                totals["jobs"] += r["jobs"]
            except Exception as e:  # noqa: BLE001 - one bad site shouldn't stop the run
                say(f"{label}: crawl failed ({e})")
        for board, slug in sorted(c.ats):
            try:
                totals["jobs"] += import_jobs(db, board, slug, company_id=cid)["jobs"]
            except (requests.RequestException, ValueError):
                pass
        if find_people and provider:
            map_people(db, cid, provider)
        learn_pattern(db, cid)
        guess_people_emails(db, cid)
        assign_hiring_managers(db, cid)

        people = db.people(cid)
        hm = sum(1 for p in people if p["hiring_for"])
        totals["people"] += len(people)
        totals["hiring_managers"] += hm
        say(f"{label}: {len(people)} people, {hm} hiring managers")

    if provider:
        totals["searches_used"] = provider.calls
    return totals


# --- export -----------------------------------------------------------------

EXPORT_FIELDS = ["company", "domain", "name", "title", "type", "value", "kind", "source_url",
                 "first_seen", "last_seen"]

PEOPLE_FIELDS = ["company", "domain", "city", "industry", "name", "title", "department", "seniority",
                 "email", "email_status", "phone", "company_phone", "linkedin_url", "hiring_for", "source"]


def export_csv(db: DB, q: str = "", type_: str | None = None, kind: str | None = None) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_FIELDS)
    for r in db.search(q, type_, kind, limit=1_000_000):
        w.writerow([r["company_name"] or r["company_key"], r["company_domain"], r["name"], r["title"],
                    r["type"], r["value"], r["kind"], r["source_url"], r["first_seen"], r["last_seen"]])
    return buf.getvalue()


def export_people_csv(db: DB, q: str = "", department: str | None = None,
                      hiring_only: bool = False) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(PEOPLE_FIELDS)
    companies = {r["id"]: r for r in db.conn.execute("SELECT * FROM companies")}
    for p in db.people(q=q, department=department, hiring_only=hiring_only, limit=1_000_000):
        co = companies[p["company_id"]]
        w.writerow([co["name"] or co["key"], co["domain"], co["city"], co["industry"], p["name"],
                    p["title"], p["department"], p["seniority"], p["email"], p["email_status"],
                    p["phone"], co["phone"], p["linkedin_url"], p["hiring_for"], p["source"]])
    return buf.getvalue()


HIRING_FIELDS = ["company", "domain", "city", "job_title", "job_location", "job_url", "board", "match",
                 "name", "title", "email", "email_status", "phone", "linkedin_url"]


def export_hiring_csv(db: DB, q: str = "") -> str:
    """One row per open role x matched contact, strongest match first."""
    from .xref import hiring_rows
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HIRING_FIELDS)
    for r in hiring_rows(db, q):
        email = r["email"] or (r["value"] if r["value"] and "@" in r["value"] else None)
        phone = r["phone"] or (r["value"] if r["value"] and "@" not in r["value"] else None)
        w.writerow([r["company_name"] or r["company_key"], r["domain"], r["city"], r["job_title"], r["location"],
                    r["job_url"], r["board"], r["reason"], r["name"], r["title"], email, r["email_status"],
                    phone, r["linkedin_url"]])
    return buf.getvalue()
