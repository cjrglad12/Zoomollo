"""Named leaders at a company, found in Apollo's people search.

Apollo's search is free (no credits) and returns each person's first name, a masked last
name ("Ro***s") and title. That is enough to ask the front desk for someone by name:
"Could I speak with Dana, your VP and Controller?" Full names and emails need enrichment,
which costs credits, so it is never done automatically.

The search runs by itself for every company Zoomollo reads, when APOLLO_API_KEY is set in
zoomollo-settings.txt. LinkedIn is never scraped: its terms forbid it and it would put your
own account at risk.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

import requests

from .db import DB, now

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
MATCH_URL = "https://api.apollo.io/api/v1/people/match"
DEFAULT_FREE_CREDITS = 50  # per month; set APOLLO_FREE_CREDITS to your plan's free allowance
SENIORITIES = ["c_suite", "vp", "director", "head"]
REFRESH_DAYS = 30

SCHEMA = """CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    apollo_id TEXT,
    first_name TEXT NOT NULL,
    last_masked TEXT,
    title TEXT,
    source TEXT NOT NULL DEFAULT 'apollo search',
    found_at TEXT NOT NULL,
    UNIQUE (company_id, apollo_id)
);
CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company_id);"""


ENRICH_COLUMNS = {"last_name": "TEXT", "email": "TEXT", "email_status": "TEXT", "linkedin_url": "TEXT",
                  "enriched_at": "TEXT", "enrich_note": "TEXT"}


def _ensure(db: DB) -> None:
    import sqlite3
    db.conn.executescript(SCHEMA)
    have = {r[1] for r in db.conn.execute("PRAGMA table_info(leads)")}
    for col, typ in ENRICH_COLUMNS.items():
        if col not in have:
            try:
                db.conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):
                    raise


def configured() -> bool:
    return bool(os.environ.get("APOLLO_API_KEY", "").strip())


def save(db: DB, company_id: int, people: list[dict], source: str = "apollo search") -> int:
    """Store people from an Apollo search result. Returns how many are new."""
    from .exclusions import is_recruiting_title
    _ensure(db)
    n = 0
    for p in people or []:
        first = (p.get("first_name") or "").strip()
        if not first or len(first) < 2 or not re.match(r"^[A-Za-z][A-Za-z'’.-]+$", first):
            continue  # "Travel", "Info" style placeholders are skipped below by title too
        title = " ".join((p.get("title") or "").split()) or None
        if is_recruiting_title(title):
            continue  # recruiters are never the people you pitch
        cur = db.conn.execute(
            "INSERT INTO leads (company_id, apollo_id, first_name, last_masked, title, source, found_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (company_id, apollo_id) DO UPDATE SET "
            "first_name = excluded.first_name, last_masked = excluded.last_masked, title = excluded.title, "
            "found_at = excluded.found_at",
            (company_id, p.get("id"), first, p.get("last_name_obfuscated") or p.get("last_name"), title, source, now()))
        n += cur.rowcount
    db.conn.execute("DELETE FROM leads WHERE company_id = ? AND first_name IN ('Travel', 'Info', 'Admin', 'Team')",
                    (company_id,))
    return n


def leads(db: DB, company_id: int) -> list[dict]:
    _ensure(db)
    return [dict(r) for r in db.conn.execute("SELECT * FROM leads WHERE company_id = ? ORDER BY id", (company_id,))]


_NOISE = {"of", "and", "the", "senior", "sr", "director", "vp", "vice", "president", "head", "chief", "officer",
          "manager", "lead", "principal", "ii", "iii", "global", "executive", "evp", "svp", "a", "for", "in", "on",
          "site", "leader", "team", "&"}
_LEAD_ORDER = {"vp": 0, "director": 0, "c_level": 1, "manager": 2}
_FRONT_OFFICE = {"sales", "marketing", "customer"}


def _words(text: str | None) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z]+", (text or "").lower()):
        if w not in _NOISE and len(w) > 1:
            out.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return out


def best(db: DB, company_id: int, fit_roles: list[str], ask_title: str = "") -> dict | None:
    """The leader most likely to own these hires: their title shares words with the role or the
    title you'd ask for, they're in the same department, then VP / director before the C-suite."""
    from .orgmap import department, seniority
    rows = leads(db, company_id)
    if not rows:
        return None
    want = set().union(*(_words(r) for r in fit_roles[:3])) | _words(ask_title)
    dept = department(fit_roles[0]) if fit_roles else "other"

    def rank(r):
        t = r["title"] or ""
        d = department(t)
        overlap = len(_words(t) & want)
        mismatch = 0 if d == dept and dept != "other" else (2 if d in _FRONT_OFFICE else 1)
        return (-overlap, mismatch, _LEAD_ORDER.get(seniority(t), 3), r["id"])

    pick = min(rows, key=rank)
    return pick if rank(pick)[0] < 0 or rank(pick)[1] < 2 else None


def label(lead: dict | None) -> str:
    """'Dana, VP, Controller'"""
    if not lead:
        return ""
    return lead["first_name"] + (f", {lead['title']}" if lead.get("title") else "")


def search(domain: str, timeout: float = 20) -> list[dict] | None:
    """Apollo's people search for a company's leaders. None when it can't run (no key, error)."""
    key = os.environ.get("APOLLO_API_KEY", "").strip()
    if not key or not domain:
        return None
    try:
        r = requests.post(SEARCH_URL, timeout=timeout,
                          headers={"x-api-key": key, "Content-Type": "application/json", "Cache-Control": "no-cache"},
                          json={"q_organization_domains_list": [domain], "person_seniorities": SENIORITIES,
                                "per_page": 25, "page": 1})
        if r.status_code != 200:
            log.info("Apollo people search for %s: HTTP %s %s", domain, r.status_code, r.text[:200])
            return None
        return r.json().get("people") or []
    except (requests.RequestException, ValueError) as e:
        log.info("Apollo people search for %s failed: %s", domain, e)
        return None


def find_for_company(db: DB, company_id: int, force: bool = False) -> int | None:
    """Look up a company's leaders once a month. Free; runs only with an API key.
    Never holds the database while waiting on Apollo."""
    if not configured():
        return None
    co = db.company_row(company_id)
    if not co or not co["domain"]:
        return None
    _ensure(db)
    last = db.conn.execute("SELECT MAX(found_at) FROM leads WHERE company_id = ?", (company_id,)).fetchone()[0]
    if last and not force:
        try:
            if datetime.fromisoformat(last) > datetime.now(timezone.utc) - timedelta(days=REFRESH_DAYS):
                return 0
        except ValueError:
            pass
    db.commit()
    people = search(co["domain"])
    if people is None:
        return None
    n = save(db, company_id, people)
    db.set_setting("leads_checked_" + str(company_id), now())
    db.commit()
    return n


def backfill(db: DB, limit: int = 40, progress=None) -> int:
    """Accounts on your desk with no named contact: look up their leaders."""
    if not configured():
        return 0
    from .hiringscore import load_desk, score_all
    _ensure(db)
    have = {r[0] for r in db.conn.execute("SELECT DISTINCT company_id FROM leads")}
    todo = [s.company_id for s in sorted(score_all(db, load_desk(db)).values(), key=lambda s: -s.total)
            if s.total > 0 and s.company_id not in have][:limit]
    found = 0
    for cid in todo:
        n = find_for_company(db, cid)
        if n is None:
            break  # key rejected or Apollo down: try again next time
        found += n
    if found and progress:
        progress(f"✓ Apollo: {found} named leaders found at {len(todo)} accounts")
    return found


# --- enrichment: full name + work email, within your free monthly credits ---------------

def monthly_allowance() -> int:
    try:
        return max(0, int(os.environ.get("APOLLO_FREE_CREDITS") or DEFAULT_FREE_CREDITS))
    except ValueError:
        return DEFAULT_FREE_CREDITS


def daily_allowance() -> int:
    """Spread the month's credits out instead of spending them all on the first day."""
    return max(1, monthly_allowance() // 20) if monthly_allowance() else 0


def _month(today: datetime | None = None) -> str:
    return (today or datetime.now(timezone.utc)).strftime("%Y-%m")


def credits_used(db: DB, day: bool = False) -> int:
    key = "apollo_credits_" + (datetime.now(timezone.utc).strftime("%Y-%m-%d") if day else _month())
    try:
        return int(db.get_setting(key) or 0)
    except ValueError:
        return 0


def _spend(db: DB) -> None:
    for day in (False, True):
        key = "apollo_credits_" + (datetime.now(timezone.utc).strftime("%Y-%m-%d") if day else _month())
        db.set_setting(key, str(credits_used(db, day) + 1))


def credits_left(db: DB) -> int:
    """Credits Zoomollo may still use this month (0 once Apollo says the free credits are gone)."""
    if db.get_setting("apollo_out_" + _month()):
        return 0
    return max(0, monthly_allowance() - credits_used(db))


def enrich(db: DB, lead_id: int, timeout: float = 20) -> dict:
    """Look up one lead's full name and work email. Uses at most one free credit; personal
    emails and phone numbers (which cost more) are never requested."""
    _ensure(db)
    lead = db.conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not lead:
        return {"ok": False, "msg": "No such lead."}
    if lead["enriched_at"]:
        return {"ok": bool(lead["last_name"]), "msg": "Already looked up.", "lead": dict(lead)}
    key = os.environ.get("APOLLO_API_KEY", "").strip()
    if not key:
        return {"ok": False, "msg": "Add APOLLO_API_KEY to zoomollo-settings.txt first."}
    if credits_left(db) <= 0:
        return {"ok": False, "msg": "No free Apollo credits left this month."}
    try:
        r = requests.post(MATCH_URL, timeout=timeout,
                          headers={"x-api-key": key, "Content-Type": "application/json", "Cache-Control": "no-cache"},
                          json={"id": lead["apollo_id"], "reveal_personal_emails": False,
                                "reveal_phone_number": False})
    except requests.RequestException as e:
        return {"ok": False, "msg": f"Apollo didn't answer ({e})."}
    if r.status_code in (402, 403, 422, 429) and re.search(r"credit|quota|limit|upgrade|plan", r.text, re.I):
        db.set_setting("apollo_out_" + _month(), now())  # stop for the month: never risk paid credits
        db.commit()
        return {"ok": False, "msg": "Apollo says the free credits are used up; enrichment paused until next month."}
    if r.status_code != 200:
        log.info("Apollo enrich %s: HTTP %s %s", lead_id, r.status_code, r.text[:200])
        return {"ok": False, "msg": f"Apollo error (HTTP {r.status_code})."}
    person = (r.json() or {}).get("person") or {}
    last = (person.get("last_name") or "").strip()
    email = (person.get("email") or "").strip().lower() or None
    if "email_not_unlocked" in (email or "") or (email and "@" not in email):
        email = None
    status = (person.get("email_status") or "").lower() or None
    if person:
        _spend(db)  # count it whenever a person came back, so the budget errs on the safe side
    db.conn.execute("UPDATE leads SET last_name = ?, email = ?, email_status = ?, linkedin_url = ?, title = COALESCE(?, title), "
                    "enriched_at = ?, enrich_note = ? WHERE id = ?",
                    (last or None, email, status, person.get("linkedin_url"), person.get("title"), now(),
                     None if person else "not found in Apollo", lead_id))
    if last:
        from .orgmap import clean_linkedin_url
        full = f"{lead['first_name']} {last}"
        db.add_person(lead["company_id"], full, person.get("title") or lead["title"], email=email,
                      email_status="found" if status == "verified" else "guessed",
                      linkedin_url=clean_linkedin_url(person.get("linkedin_url") or ""), source="apollo")
    db.commit()
    return {"ok": bool(last), "msg": (f"{lead['first_name']} {last}" + (f" · {email}" if email else " · no work email on file"))
            if last else "Apollo had no more on this person (no credit used).",
            "lead": dict(db.conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone())}


def auto_enrich(db: DB, limit: int | None = None, progress=None) -> int:
    """Best leads at your highest-scoring accounts that have no email yet, a few per day."""
    if not configured():
        return 0
    from .hiringscore import load_desk, score_all
    _ensure(db)
    room = min(credits_left(db), max(0, daily_allowance() - credits_used(db, day=True)))
    if limit is not None:
        room = min(room, limit)
    done = 0
    for s in sorted(score_all(db, load_desk(db)).values(), key=lambda s: -s.total):
        if done >= room or s.total <= 0 or s.offlimits.blocked or not s.fit_roles:
            continue
        if db.conn.execute("SELECT 1 FROM people WHERE company_id = ? AND email IS NOT NULL", (s.company_id,)).fetchone():
            continue  # already have someone to email there
        lead = best(db, s.company_id, s.fit_roles)
        if not lead or lead.get("enriched_at"):
            continue
        r = enrich(db, lead["id"])
        if "free credits" in r["msg"] or "didn't answer" in r["msg"] or "error" in r["msg"]:
            break
        done += 1
    if done and progress:
        progress(f"✓ Apollo: looked up {done} leaders ({credits_left(db)} free credits left this month)")
    return done
