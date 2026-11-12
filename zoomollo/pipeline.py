"""Glue: discover companies, crawl them, pull jobs, map people, save it all."""

from __future__ import annotations

import csv
import html
import io
import logging
import re
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests

from .crawler import Crawler, normalize_domain
from .db import DB, now, now_precise
from .exclusions import is_staffing_domain, is_support_title, staffing_signal
from .extract import (_GENERIC_NAME, _domain_match, best_company_name, is_role_email, looks_like_name,
                      normalize_phone, plausible_title, social_kind)
from .jobboards import fetch_jobs
from .patterns import PATTERNS, has_mx, infer_pattern, render, split_name

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

_CUSTOMER_PAGES = re.compile(
    r"/(white-?papers?|partners?|customers?|customer-stories|success-stories|case-stud(y|ies)|testimonials?|reviews?|"
    r"proof|clients?|portfolio|webinars?|events?)(/|$)", re.I)


def quoted_customer_page(url: str) -> bool:
    """Pages where the people named are customers and partners being quoted, not the company's own staff."""
    return bool(_CUSTOMER_PAGES.search(urlparse(url or "").path))


_JOB_PAGES = re.compile(r"/(jobs?|job-openings?|positions?|openings?|vacanc(?:y|ies))/[^/]+", re.I)
_PEOPLE_PAGES = re.compile(r"(team|leadership|people|staff|about|who-we-are|management|partners|directory|bios?)", re.I)


def people_page(url: str) -> bool:
    """A team, leadership or about page: the names on it are staff even when the text under them isn't a title."""
    return bool(_PEOPLE_PAGES.search(urlparse(url or "").path))
_ARTICLE_PAGES = re.compile(
    r"/(resources?|blogs?|news|press|press-releases?|insights?|thoughts?|posts?|articles?|advisor(?:y|ies)|"
    r"threat[\w-]*|white-?papers?|case-stud(?:y|ies)|events?|webinars?|podcasts?|media)(/|$)", re.I)
_BIO_VERBS = re.compile(r"\b(began|joined|worked|career|started|previously|prior|was|is|has|had|spent|served|years|"
                        r"before|after|since|earned|graduated|holds|leads|brings)\b", re.I)
_AT_ORG = re.compile(r"\bat\s+((?:[A-Z][\w&'’.-]*\s?){1,4})")


def job_posting_page(url: str) -> bool:
    """One job's own page: its headings ("Job Summary", "Program Leadership") are not people."""
    return bool(_JOB_PAGES.search(urlparse(url or "").path))


def article_page(url: str) -> bool:
    """Blog posts, news, advisories and the like: numbers in them are rarely the company's phone."""
    return bool(_ARTICLE_PAGES.search(urlparse(url or "").path))


def quoted_outsider(name: str, page_text: str, company: str | None, domain: str | None) -> bool:
    """'Angie Lombardi, Vice President of Marketing at Franklin Group' on Grace Hill's site:
    someone introduced as working at another organisation is a customer or guest, not staff."""
    text = page_text or ""
    i = text.lower().find((name or "").lower())
    if not name or i < 0:
        return False
    after = re.split(r"[.!?\"“”]\s", text[i + len(name): i + len(name) + 110])[0]
    m = _AT_ORG.search(after)
    if not m or _BIO_VERBS.search(after[:m.start()]) or len(after[:m.start()].split()) > 9:
        return False
    own = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", (company or "") + " " + (domain or "").split(".")[0])}
    org = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", m.group(1))}
    return bool(org) and not (org & own) and not any(o in (domain or "") for o in org)


def crawl_company(db: DB, domain: str, max_pages: int = 40, delay: float = 1.0,
                  region: str = "US", progress: Progress | None = None,
                  import_ats: bool = True) -> dict:
    domain = normalize_domain(domain)
    found = {"pages": 0, "emails": 0, "phones": 0, "people": 0, "jobs": 0, "blocked": None, "outlinks": []}
    if db.is_blocked(domain) or is_staffing_domain(domain):
        found["blocked"] = "staffing / recruiting company"
        return found
    company_id = db.company(domain, domain=domain)
    # Never hold the database while waiting on a website: a call being logged on your phone
    # must not wait behind a slow page. Save before every stretch of network reading.
    db.commit()
    run_started = now_precise()
    crawler = Crawler(max_pages=max_pages, delay=delay, region=region)
    ats: set[tuple[str, str]] = set()
    name_from: tuple | None = None  # (path depth, site name, title) of the page nearest the homepage
    said_headcount: list[int] = []  # headcounts the site states about itself

    for page in crawler.crawl(domain):
        found["pages"] += 1
        if progress:
            progress(f"{page.url}: {len(page.emails)} emails, {len(page.phones)} phones")
        if urlparse(page.url).path in ("", "/", "/about", "/about-us"):
            row = db.company_row(company_id)
            why = staffing_signal(page.title, page.description, row["name"] if row else None, domain=domain)
            if why:  # a staffing / recruiting firm: drop everything and never come back
                db.block_company(company_id, why)
                found["blocked"] = why
                return found
        if (page.title or page.site_name) and urlparse(page.url).path in ("", "/"):
            row = db.company_row(company_id)
            if not row["name"] or row["name"] in (row["key"], domain) or _GENERIC_NAME.match(row["name"]):
                db.update_company(company_id, name=best_company_name(domain, page.site_name, page.title))
        ats |= page.ats
        if urlparse(page.url).path in ("", "/") and page.description:
            row_ = db.company_row(company_id)
            if row_ and row_["summary_source"] not in ("you", "curated"):  # a line you wrote is never overwritten
                from .extract import tight_summary
                line = tight_summary(page.description, row_["name"] or page.site_name)
                if line and line != row_["summary"]:
                    db.update_company(company_id, summary=line, summary_source="website")
        if page.headcount:
            said_headcount.append(page.headcount)
        if not (article_page(page.url) or job_posting_page(page.url)):
            from .geo import offshore_phrase
            phrase = offshore_phrase(getattr(page, "text", "") or "")
            if phrase and not (db.company_row(company_id) or {"abroad_note": 1})["abroad_note"]:
                db.update_company(company_id, abroad_note=phrase)
        path = urlparse(page.url).path.strip("/")
        if (page.title or page.site_name) and (name_from is None or path.count("/") < name_from[0]):
            name_from = (path.count("/") + (1 if path else 0), page.site_name, page.title)

        named = {p.email: p for p in page.people if p.email}
        # Until the company is named, its website name ("firemon") stops "Choose FireMon" being saved as a person.
        cname = (db.company_row(company_id) or {"name": None})["name"] or domain.split(".")[0]
        for email in page.emails:
            p = named.get(email)
            name = p.name if p and looks_like_name(p.name, cname) else None
            if db.add_contact(company_id, "email", email, _kind(email), name=name,
                              title=p.title if p else None, source_url=page.url):
                found["emails"] += 1
        support = getattr(page, "support_phones", set())
        person_phones = {p.phone for p in page.people if p.phone}
        for number in support - person_phones:
            # Customer service / tech support: never a way to a hiring manager. Remembered so
            # it doesn't come back from another page.
            if not db.is_suppressed(number):
                db.suppress(number, "customer service line")
        for p in page.people:
            if p.phone and is_support_title(p.title):
                p.phone = None
            if p.phone and p.name:
                db.add_contact(company_id, "phone", p.phone, "person",
                               name=p.name if looks_like_name(p.name, cname) else None,
                               title=p.title, source_url=page.url)
            own_email = bool(p.email and p.email.lower().endswith("@" + domain))
            if quoted_customer_page(page.url) and not own_email:
                continue  # a customer quoted in a case study or white paper doesn't work here
            if job_posting_page(page.url) and not own_email:
                continue  # headings on a job post ("Job Summary") are not people
            if p.name and (p.name.strip().lower() == cname.strip().lower()
                           or (not own_email and quoted_outsider(p.name, getattr(page, "text", "") or "", cname, domain))):
                continue  # the company itself, or someone introduced as working somewhere else
            if p.title:
                p.title = " ".join(p.title.split())
            if p.title and not plausible_title(p.title) and people_page(page.url):
                p.title = None  # a bio sentence sat where the title goes: keep the person, drop the sentence
            if p.name and looks_like_name(p.name, cname) and plausible_title(p.title):
                from .orgmap import clean_linkedin_url
                if db.add_person(company_id, p.name, p.title, email=p.email, phone=p.phone,
                                 linkedin_url=clean_linkedin_url(p.linkedin or ""), source="website",
                                 source_url=page.url):
                    found["people"] += 1
        for phone in ([] if article_page(page.url) or job_posting_page(page.url) else page.phones - support):
            if db.add_contact(company_id, "phone", phone, "company", source_url=page.url):
                found["phones"] += 1
        for url in page.socials:
            db.add_social(company_id, social_kind(url) or "other", url)
        for link in page.links:  # other businesses this site links to (partners, suppliers, clients)
            d = urlparse(link).netloc.lower().removeprefix("www.")
            if d and d != domain and not d.endswith("." + domain) and d not in found["outlinks"]:
                found["outlinks"].append(d)
        for j in page.jobs:
            db.add_job(company_id, "website", j["external_id"], j["title"], j["location"], j["url"],
                       j["posted_at"], j["description"])
            found["jobs"] += 1
        db.commit()

    row = db.company_row(company_id)
    if said_headcount and (not row["headcount"] or row["headcount_source"] == "website"):
        # What the site says most often; a figure you typed or one from Apollo is never overwritten.
        top = max(set(said_headcount), key=lambda n: (said_headcount.count(n), n))
        db.update_company(company_id, headcount=top, headcount_source="website")
    if name_from and (not row["name"] or row["name"] in (row["key"], domain) or _GENERIC_NAME.match(row["name"])):
        # The homepage redirected (e.g. to /en-us/), so name it from the page nearest the top.
        db.update_company(company_id, name=best_company_name(domain, name_from[1], name_from[2]))
    loaded = {"website"} if found["pages"] else set()  # sources that answered this time
    db.commit()
    if import_ats and not ats:
        ats |= find_job_boards(crawler, domain)
    if import_ats:
        for board, slug in sorted(ats):
            try:
                found["jobs"] += import_jobs(db, board, slug, domain=domain, mark_checked=False)["jobs"]
                loaded.add(board)
            except (requests.RequestException, ValueError) as e:
                log.info("job board %s:%s failed: %s", board, slug, e)

    found["closed"] = reconcile_jobs(db, company_id, run_started, loaded)
    learn_pattern(db, company_id)
    db.commit()
    guess_people_emails(db, company_id)  # people found on the site get guessed work emails too
    if found["pages"]:
        db.update_company(company_id, last_crawled=now(), last_checked=now())
    db.commit()
    assign_hiring_managers(db, company_id)
    try:  # named leaders from Apollo's free people search (only with APOLLO_API_KEY set)
        from .leads import find_for_company
        find_for_company(db, company_id)
    except Exception as e:  # noqa: BLE001 - a lookup must never stop a crawl
        log.info("leader lookup for %s failed: %s", domain, e)
    return found


def reconcile_jobs(db: DB, company_id: int, run_started: str, sources: set[str]) -> int:
    """Roles not seen this time (from a source that did load) get a strike; two strikes
    in a row and the role is marked closed. Returns how many closed."""
    co = db.company_row(company_id)
    if not sources or not co or not co["last_checked"]:
        return 0  # first look at this company, or nothing loaded: nothing to compare against
    marks = ",".join("?" * len(sources))
    db.conn.execute(
        f"UPDATE jobs SET missed_checks = missed_checks + 1 WHERE company_id = ? AND closed_at IS NULL "
        f"AND board IN ({marks}) AND COALESCE(last_seen, first_seen) < ?", (company_id, *sources, run_started))
    gone = db.conn.execute("SELECT id, title FROM jobs WHERE company_id = ? AND closed_at IS NULL AND missed_checks >= 2",
                           (company_id,)).fetchall()
    for j in gone:
        db.conn.execute("UPDATE jobs SET closed_at = ? WHERE id = ?", (now(), j["id"]))
        db.add_event(company_id, "job_gone", f"Role closed or filled: {j['title']}", job_id=j["id"])
    db.commit()
    return len(gone)


_CAREERS_LINK = re.compile(r"(?i)career|/jobs\b|job-?search|job-?openings|current-?openings|join-?(us|our)")


def find_job_boards(crawler: Crawler, domain: str, max_fetches: int = 8) -> set[tuple[str, str]]:
    """Follow the site's Careers links, even to another host (careers.firm.com, a job
    portal), up to two clicks deep, and spot which job board holds its openings.
    Big sites spend a whole crawl on bios and never get there."""
    from .extract import find_ats
    found: set[tuple[str, str]] = set()
    queue = [f"https://{domain}", f"https://{domain}/careers"]
    seen: set[str] = set()
    fetches = 0
    while queue and fetches < max_fetches and not found:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        got = crawler.fetch(url)
        fetches += 1
        if not got:
            continue
        final_url, html_text = got
        found |= find_ats(final_url) | find_ats(html_text)
        for href in re.findall(r'href=["\']([^"\'#]+)', html_text):
            link = urljoin(final_url, html.unescape(href))
            if _CAREERS_LINK.search(link) and link.startswith("http") and link not in seen:
                queue.append(link)
    return found


def learn_pattern(db: DB, company_id: int) -> str | None:
    """Learn the email format from named emails, and from known people whose
    names line up with addresses found anywhere on the site."""
    examples = db.named_emails(company_id)
    row = db.company_row(company_id)
    domain = row["domain"] if row else None
    if domain:
        locals_ = {r[0].split("@")[0]: r[0] for r in db.conn.execute(
            # Our own guesses aren't evidence: matching them back would mark every guess "found".
            "SELECT value FROM contacts WHERE company_id = ? AND type = 'email' AND value LIKE ? "
            "AND COALESCE(kind, '') != 'guessed'",
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
                region: str = "US", company_id: int | None = None, mark_checked: bool = True) -> dict:
    run_started = now_precise()
    jobs = fetch_jobs(board, slug, region)
    if company_id is None:
        key = normalize_domain(domain) if domain else f"{board}:{slug}"
        # The board's address is only a stand-in name when there's no website to name it from.
        company_id = db.company(key, domain=normalize_domain(domain) if domain else None,
                                name=None if domain else slug)
    emails = phones = 0
    for j in jobs:
        db.add_job(company_id, j.board, j.external_id, j.title, j.location, j.url, j.posted_at, j.description)
        for e in j.emails:
            emails += db.add_contact(company_id, "email", e, _kind(e), source_url=j.url)
        for p in j.phones:
            phones += db.add_contact(company_id, "phone", p, "company", source_url=j.url)
    db.commit()
    if mark_checked:  # standalone import (not part of a site crawl)
        reconcile_jobs(db, company_id, run_started, {board})
        db.update_company(company_id, last_checked=now())
        db.commit()
    return {"jobs": len(jobs), "emails": emails, "phones": phones}


# --- people -----------------------------------------------------------------

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


def fill_missing_guesses(db: DB, progress: Progress | None = None) -> int:
    """Give a first email guess to everyone who still has none, at every company with
    a website that takes email. Catches people saved before guessing ran, or while a
    DNS lookup was failing. Safe to run often: people already guessed are skipped."""
    rows = db.conn.execute(
        "SELECT DISTINCT p.company_id FROM people p JOIN companies c ON c.id = p.company_id "
        "WHERE p.email IS NULL AND (p.email_status IS NULL OR p.email_status != 'failed') "
        "AND c.domain IS NOT NULL AND c.domain != ''").fetchall()
    n = 0
    for r in rows:
        try:
            n += guess_people_emails(db, r[0])
        except Exception as e:  # noqa: BLE001 - one bad company shouldn't stop the rest
            log.info("guessing at company %s failed: %s", r[0], e)
    if n and progress:
        progress(f"✓ Email guesses filled in for {n} people who had none")
    return n


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
             use_osm: bool = True, use_hubs: bool = True, use_sources: bool = True,
             use_records: bool = True, follow_links: bool = True, progress: Progress | None = None,
             crawler=None) -> dict:
    """'Commercial HVAC' + 'Austin, TX' -> companies, jobs, people, hiring managers, emails.

    No search engines or paid services: companies come from map data, business-group
    member directories, public data files you've added, and links between company sites.
    """
    from collections import deque

    from .crawler import Crawler
    from .discover import Candidate, check_site, find_companies, guess_website, is_company_domain

    say = progress or (lambda m: None)
    crawler = crawler or Crawler(max_pages=40, delay=1.0)
    say(f"Looking for {what} in {where}…")
    cands = find_companies(what, where, limit, use_osm=use_osm, use_hubs=use_hubs, crawler=crawler,
                           progress=say)
    if use_records:
        # License lists and building permits on the city's / state's open-data portals.
        from .opendata import find_public_records
        try:
            records, _ = find_public_records(what, where, progress=say)
        except Exception as e:  # noqa: BLE001 - optional source
            records = []
            say(f"Public records unavailable ({e})")
        say(f"Public records: {len(records)} matching businesses")
        known = {c.key for c in cands} | {c.domain for c in cands if c.domain}
        records = [r for r in records if r.key not in known]
        # Alternate, so the most active licensed / permitted businesses aren't crowded out.
        mixed = []
        for i in range(max(len(cands), len(records))):
            mixed += cands[i:i + 1] + records[i:i + 1]
        cands = mixed
    if use_sources:
        from .datasources import from_source
        for src in db.sources():
            try:
                rows = from_source(src["url"], what, where, src["label"])
                say(f"{src['label']}: {len(rows)} matching records")
                db.conn.execute("UPDATE data_sources SET last_used = ? WHERE id = ?", (now(), src["id"]))
                cands += rows
            except Exception as e:  # noqa: BLE001 - a broken file shouldn't stop the run
                say(f"{src['label']}: couldn't read ({e})")
    say(f"Found {len(cands)} candidate companies")

    totals = {"companies": 0, "people": 0, "hiring_managers": 0, "jobs": 0, "emails": 0, "phones": 0,
              "skipped_staffing": 0, "found_via_links": 0}
    queue = deque(cands)
    seen = {c.key for c in cands} | {c.domain for c in cands if c.domain}
    checks = 0

    while queue and totals["companies"] < limit:
        c = queue.popleft()
        if db.is_blocked(c.key, c.domain) or staffing_signal(c.name, domain=c.domain):
            totals["skipped_staffing"] += 1
            continue
        if c.needs_check:
            if checks >= limit * 6:
                continue
            checks += 1
            ok, page_name = check_site(crawler, c.domain, what, where)
            if not ok:
                continue
            c.name = page_name or c.name
            totals["found_via_links"] += 1
        if not c.domain:
            c.domain = guess_website(crawler, c.name, where, c.phone, seen)
            if c.domain:
                if c.domain in seen and c.key != c.domain:
                    continue  # already have that site under another listing
                seen.add(c.domain)
                c.sources.add("website guessed")

        cid = db.company(c.key, domain=c.domain, name=c.name)
        db.update_company(cid, city=where, industry=what, source=", ".join(sorted(c.sources)) or None,
                          **{k: v for k, v in {"address": c.address, "phone": c.phone}.items() if v})
        if c.phone and (e164 := normalize_phone(c.phone)):
            db.add_contact(cid, "phone", e164, "company", source_url=next(iter(c.sources), None))
        if c.email:
            db.add_contact(cid, "email", c.email, _kind(c.email), source_url=next(iter(c.sources), None))
        if c.contact_name:
            db.add_person(cid, c.contact_name, c.contact_title, source="public record")
        db.commit()
        totals["companies"] += 1
        label = f"[{totals['companies']}/{limit}] {c.name}" + (f" ({c.domain})" if c.domain else "")

        if c.domain:
            try:
                r = crawl_company(db, c.domain, max_pages=crawl_pages, delay=1.0)
                if r.get("blocked"):
                    say(f"{label}: skipped, staffing/recruiting company ({r['blocked']})")
                    totals["companies"] -= 1
                    totals["skipped_staffing"] += 1
                    continue
                totals["emails"] += r["emails"]
                totals["phones"] += r["phones"]
                totals["jobs"] += r["jobs"]
                if follow_links:
                    for d in r["outlinks"]:
                        if d not in seen and is_company_domain(d) and not db.is_blocked(d):
                            seen.add(d)
                            queue.append(Candidate(name=d, domain=d, sources={f"linked from {c.domain}"},
                                                   needs_check=True))
            except Exception as e:  # noqa: BLE001 - one bad site shouldn't stop the run
                say(f"{label}: crawl failed ({e})")
        for board, slug in sorted(c.ats):
            try:
                totals["jobs"] += import_jobs(db, board, slug, company_id=cid)["jobs"]
            except (requests.RequestException, ValueError):
                pass
        learn_pattern(db, cid)
        guess_people_emails(db, cid)
        assign_hiring_managers(db, cid)

        people = db.people(cid)
        hm = sum(1 for p in people if p["hiring_for"])
        totals["people"] += len(people)
        totals["hiring_managers"] += hm
        say(f"{label}: {len(people)} people, {hm} hiring managers")

    db.commit()
    return totals


_NOT_EMPLOYER_NAME = re.compile(r"chamber|association|council|angels|investor network|small business development|"
                                r"\bsbdc\b|accelerat|incubat|1 million cups|foundation|innovation quarter|"
                                r"economic development", re.I)


def crawl_directory(db: DB, url: str, what: str, where: str, limit: int = 40, crawl_pages: int = 15,
                    progress: Progress | None = None) -> dict:
    """Companies listed on a directory site (an industry association's member list, a
    sponsor page, an incubator's portfolio). Each linked company website is checked
    against 'what' (if given: every word must appear on its homepage), then saved
    under that search and city and crawled like any other prospect."""
    from urllib.parse import urlparse

    from .crawler import Crawler
    from .discover import check_site, hub_members

    say = progress or (lambda m: None)
    url = url if "://" in url else "https://" + url
    hub = normalize_domain(url)
    path = urlparse(url).path or "/"
    crawler = Crawler(max_pages=200, delay=1.0)
    # Directories are often split into A-Z pages and one page per member: read deep.
    members = hub_members(crawler, hub, what or "", max_pages=200, start_paths=[path])
    crawler.max_pages = 40
    say(f"{hub}: {len(members)} linked company sites")
    totals = {"companies": 0, "people": 0, "jobs": 0, "checked": 0, "skipped": 0}
    label = what or "directory: " + hub
    for c in members:
        if totals["companies"] >= limit or totals["checked"] >= limit * 4:
            break
        if db.is_blocked(c.key, c.domain) or staffing_signal(c.name, domain=c.domain):
            totals["skipped"] += 1
            continue
        totals["checked"] += 1
        # Always look at the homepage: it weeds out staffing firms and gives a proper name.
        ok, page_name = check_site(crawler, c.domain, what, where, trusted_location=True)
        if not ok:
            continue
        if _NOT_EMPLOYER_NAME.search(" ".join(x for x in [c.name, page_name] if x)):
            totals["skipped"] += 1  # other business groups, investor networks, incubators
            continue
        # The directory's link text is used only if it looks like the company ("Qentelli" for
        # qentelli.com), not a button or headline ("Visit my Company").
        anchor_ok = c.name and c.name != c.domain and _domain_match(c.name, c.domain) >= 2 \
            and not _GENERIC_NAME.match(c.name)
        name = c.name if anchor_ok else (page_name or best_company_name(c.domain))
        cid = db.company(c.key, domain=c.domain, name=name)
        db.update_company(cid, city=where, industry=label, source=f"listed on {hub}")
        db.commit()
        totals["companies"] += 1
        try:
            r = crawl_company(db, c.domain, max_pages=crawl_pages, delay=1.0)
            if r.get("blocked"):
                totals["companies"] -= 1
                continue
            totals["jobs"] += r["jobs"]
        except Exception as e:  # noqa: BLE001 - one bad site shouldn't stop the run
            say(f"{name}: crawl failed ({e})")
        n = len(db.people(cid))
        totals["people"] += n
        say(f"[{totals['companies']}] {name} ({c.domain}): {n} people")
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
