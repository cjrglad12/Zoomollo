"""Loxo connection: off-limits data in, contacts out.

Needs LOXO_AGENCY_SLUG and LOXO_API_KEY in zoomollo-settings.txt (Loxo:
Settings > API Keys). LOXO_DOMAIN defaults to app.loxo.co.

Endpoints and paging follow Loxo's open API as used by the open-source
loxo-cli (verified there against a live agency):
  GET  /api/<slug>/companies   -> {"companies": [...], "scroll_id": ...}  (scroll paging)
  GET  /api/<slug>/jobs        -> {"results": [...], "current_page", "total_pages"}  (page paging)
  GET  /api/<slug>/placements  -> {"placements": [...], "scroll_id": ...}  (scroll paging)
  POST /api/<slug>/people      body {"person": {...}}
"""

from __future__ import annotations

import os
import re
import time

import requests

from .db import DB, now

# Job statuses that mean the company is an active client. Anything else
# (closed, filled, cancelled, lost...) counts as a past job.
OPEN_STATUS = re.compile(r"active|open|hot|sourcing|interview|offer|on ?hold|hold|pending|new|search", re.I)
CLOSED_STATUS = re.compile(r"closed|filled|cancel|lost|inactive|archiv|dead|complete|declin", re.I)

# Responses worth retrying. 429 is Loxo's rate limit; the others are transient.
RETRY_STATUS = (429, 500, 502, 503, 504)


def request_with_backoff(call, retries: int = 5):
    """Run one HTTP call, retrying dropped connections, rate limits and server errors.

    On 429, wait for a numeric Retry-After header when Loxo sends one. Otherwise
    the wait doubles each try (1s, 2s, 4s...). The last response is returned as-is
    so the caller can raise.
    """
    for attempt in range(retries):
        try:
            r = call()
        except (requests.ConnectionError, requests.Timeout):
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
            continue
        if r.status_code in RETRY_STATUS and attempt < retries - 1:
            wait = r.headers.get("Retry-After")
            time.sleep(float(wait) if wait and str(wait).replace(".", "", 1).isdigit() else 2 ** attempt)
            continue
        return r


class LoxoClient:
    def __init__(self, agency_slug: str | None = None, api_key: str | None = None,
                 domain: str | None = None):
        slug, slug_domain = parse_slug(agency_slug or os.environ.get("LOXO_AGENCY_SLUG") or "")
        self.agency_slug = slug
        self.api_key = (api_key or os.environ.get("LOXO_API_KEY") or "").strip()
        self.domain = domain or os.environ.get("LOXO_DOMAIN") or slug_domain or "app.loxo.co"
        if not (self.agency_slug and self.api_key):
            raise RuntimeError("Add LOXO_AGENCY_SLUG and LOXO_API_KEY to zoomollo-settings.txt to connect Loxo.")
        self.base = f"https://{self.domain}/api/{self.agency_slug}"
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"})

    def get(self, endpoint: str, retries: int = 5, **params):
        """GET with retries for dropped connections, rate limits (429) and server errors."""
        r = request_with_backoff(
            lambda: self.session.get(f"{self.base}/{endpoint}", params=params, timeout=30), retries=retries)
        r.raise_for_status()
        return r.json()

    def scroll(self, endpoint: str, key: str, max_pages: int = 5000):
        """Endpoints paged with scroll_id (companies, placements)."""
        params: dict = {}
        last = None
        for _ in range(max_pages):
            data = self.get(endpoint, **params)
            items = data.get(key) if isinstance(data, dict) else data
            if not items:
                return
            yield from items
            sid = data.get("scroll_id") if isinstance(data, dict) else None
            if not sid or sid == last:
                return
            last = params["scroll_id"] = sid

    def pages(self, endpoint: str, key: str = "results", per_page: int = 100, max_pages: int = 2000):
        """Endpoints paged with page numbers (jobs)."""
        for page in range(1, max_pages + 1):
            data = self.get(endpoint, page=page, per_page=per_page)
            items = data.get(key) if isinstance(data, dict) else data
            if not items:
                return
            yield from items
            total = data.get("total_pages") or (data.get("pagination") or {}).get("total_pages")
            if total and page >= int(total):
                return

    def create_person(self, payload: dict) -> dict:
        """POST a person. Same rate-limit backoff as GET: Retry-After, then exponential wait."""
        r = request_with_backoff(lambda: self.session.post(f"{self.base}/people", json=payload, timeout=30))
        r.raise_for_status()
        return r.json()


def parse_slug(value: str) -> tuple[str, str | None]:
    """Accept the agency slug however it was pasted: "acme", "app.loxo.co/acme",
    "https://app.loxo.co/acme/dashboard" or "https://app.loxo.co/api/acme"."""
    v = value.strip().strip("/")
    if "/" not in v and "." not in v:
        return v, None
    v = re.sub(r"^https?://", "", v)
    parts = [x for x in v.split("/") if x]
    domain = parts[0] if parts and "." in parts[0] else None
    rest = parts[1:] if domain else parts
    if rest and rest[0] == "api":
        rest = rest[1:]
    return (rest[0] if rest else ""), domain


FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com", "me.com",
             "live.com", "msn.com", "comcast.net", "att.net", "sbcglobal.net", "verizon.net", "ymail.com",
             "protonmail.com", "proton.me", "gmx.com", "mail.com", "bellsouth.net", "cox.net", "charter.net"}


def _domain_from_emails(emails) -> str | None:
    """Company domain from a saved address like jane@acme.com (not gmail etc.)."""
    for e in emails or []:
        addr = e.get("value") or e.get("email") if isinstance(e, dict) else e
        if isinstance(addr, str) and "@" in addr:
            d = addr.rsplit("@", 1)[1].strip().lower()
            if d and d not in FREE_MAIL:
                return d
    return None


def configured() -> bool:
    return bool(os.environ.get("LOXO_AGENCY_SLUG") and os.environ.get("LOXO_API_KEY"))


# --- off-limits sync --------------------------------------------------------------

def _status_name(status) -> str:
    if isinstance(status, dict):
        return str(status.get("name") or "")
    return str(status or "")


def job_is_open(job: dict) -> bool:
    name = _status_name(job.get("status"))
    if not name:
        return True  # no status: assume it's live rather than miss a client
    return bool(OPEN_STATUS.search(name)) and not CLOSED_STATUS.search(name)


def sync(db: DB, client: LoxoClient | None = None, progress=None, full: bool = True) -> dict:
    """Refresh the local copy of Loxo data used for off-limits checks.

    full=True reads every company (status, block flag, owner, domains) plus jobs
    and placements: thorough but slow on big accounts (~15 min for 24k companies).
    full=False (quick) re-reads only jobs and placements, about 30 seconds, and
    keeps company details from the last full sync.
    """
    from .crawler import normalize_domain
    from .orgmap import norm_company

    say = progress or (lambda m: None)
    client = client or LoxoClient()
    companies: dict[int, dict] = {}
    if not full:
        for r in db.conn.execute("SELECT * FROM loxo_companies"):
            companies[r["loxo_id"]] = {"name": r["name"], "url": r["domain"], "open": 0, "all": 0, "placements": 0,
                                       "last": None, "status": r["status_name"], "blocked": r["blocked"],
                                       "until": r["blocked_until"], "owner": r["owner"]}

    def entry(cid, name=None, url=None):
        e = companies.setdefault(int(cid), {"name": None, "url": None, "open": 0, "all": 0, "placements": 0,
                                            "last": None, "status": None, "blocked": 0, "until": None,
                                            "owner": None})
        e["name"] = e["name"] or name
        e["url"] = e["url"] or url
        return e

    for c in (client.scroll("companies", "companies") if full else []):
        if c.get("id") is None:
            continue
        e = entry(c["id"], c.get("name"), c.get("url") or _domain_from_emails(c.get("emails")))
        st = c.get("company_global_status")
        e["status"] = st.get("name") if isinstance(st, dict) else (st or None)
        e["blocked"] = 1 if c.get("blocked") else 0
        e["until"] = (c.get("blocked_until") or "")[:10] or None
        e["owner"] = c.get("owned_by_name")
    if full:
        say(f"Loxo companies: {len(companies)}")

    jobs = 0
    for j in client.pages("jobs", "results"):
        co = j.get("company") or {}
        if not isinstance(co, dict) or co.get("id") is None:
            continue
        e = entry(co["id"], co.get("name"), co.get("url"))
        e["all"] += 1
        e["open"] += 1 if job_is_open(j) else 0
        jobs += 1
    say(f"Loxo jobs: {jobs}")

    placements = 0
    for p in client.scroll("placements", "placements"):
        co = ((p.get("job") or {}).get("company") or {}) if isinstance(p.get("job"), dict) else {}
        if not isinstance(co, dict) or co.get("id") is None:
            continue
        e = entry(co["id"], co.get("name"), co.get("url"))
        e["placements"] += 1
        start = (p.get("start_date") or p.get("created_at") or "")[:10]
        if start and (not e["last"] or start > e["last"]):
            e["last"] = start
        placements += 1
    say(f"Loxo placements: {placements}")

    t = now()
    db.conn.execute("DELETE FROM loxo_companies")
    for cid, e in companies.items():
        domain = normalize_domain(e["url"]) if e["url"] and "." in e["url"] else None
        db.conn.execute(
            "INSERT INTO loxo_companies (loxo_id, name, name_norm, domain, open_jobs, all_jobs, last_placement, "
            "placements, status_name, blocked, blocked_until, owner, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, e["name"], norm_company(e["name"] or ""), domain, e["open"], e["all"], e["last"],
             e["placements"], e["status"], e["blocked"], e["until"], e["owner"], t))
    db.set_setting("loxo_synced_at", t)
    if full:
        db.set_setting("loxo_full_synced_at", t)
    db.commit()
    return {"companies": len(companies), "jobs": jobs, "placements": placements, "full": full}


def auto_sync(db: DB, progress=None, quick_hours: float = 6, full_days: float = 7) -> dict | None:
    """Sync if it's due: a full sync weekly (or if never done), a quick one every few hours."""
    from datetime import datetime, timedelta, timezone
    if not configured():
        return None

    def older(key, delta):
        v = db.get_setting(key)
        try:
            when = datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None
            if when and when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return not when or when < datetime.now(timezone.utc) - delta
        except ValueError:
            return True
    if older("loxo_full_synced_at", timedelta(days=full_days)):
        return sync(db, progress=progress, full=True)
    if older("loxo_synced_at", timedelta(hours=quick_hours)):
        return sync(db, progress=progress, full=False)
    return None


# --- pushing contacts ---------------------------------------------------------------

def person_payload(name: str, email: str | None, phone: str | None, title: str | None,
                   company: str | None) -> dict:
    p = {"name": name}
    if email:
        p["email"] = email
    if phone:
        p["phone"] = phone
    if title:
        p["title"] = title
    if company:
        p["company"] = company
    return {"person": p}


def push_people(db: DB, dry_run: bool = True, include_guessed: bool = False,
                hiring_only: bool = False, limit: int = 100) -> list[dict]:
    """Push mapped people (with an email) not pushed yet. Returns the payloads.
    Off-limits companies are skipped."""
    from .offlimits import status as offlimits_status
    sql = ("SELECT p.*, co.name AS company_name, co.key AS company_key FROM people p "
           "JOIN companies co ON co.id = p.company_id WHERE p.email IS NOT NULL "
           "AND p.pushed_to_loxo = 0")
    if not include_guessed:
        sql += " AND p.email_status IN ('found', 'confirmed')"
    if hiring_only:
        sql += " AND p.hiring_for IS NOT NULL"
    rows = db.conn.execute(sql + " LIMIT ?", (limit,)).fetchall()
    client = None if dry_run else LoxoClient()
    sent = []
    for r in rows:
        if offlimits_status(db, r["company_id"]).blocked:
            continue
        payload = person_payload(r["name"], r["email"], r["phone"], r["title"],
                                 r["company_name"] or r["company_key"])
        if client:
            client.create_person(payload)
            db.conn.execute("UPDATE people SET pushed_to_loxo = 1 WHERE id = ?", (r["id"],))
            db.commit()
        sent.append(payload)
    return sent
