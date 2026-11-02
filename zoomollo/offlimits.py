"""Off-limits check: is it OK to prospect this company?

Rules (contingency-desk defaults):
  blocked  - blocked in Loxo (Loxo's own block flag, until its end date if set)
  blocked  - Loxo company status says it's a client ("Current Client")
  blocked  - an open / on-hold job with this company in Loxo (active client)
  blocked  - a placement there within the last OFFLIMITS_MONTHS months (default 12)
  blocked  - marked off-limits by you in Zoomollo (optionally until a date)
  warn     - an active opportunity in Loxo (someone is working it), allowed
  warn     - a past client in Loxo (older jobs / placements): warm re-engagement, allowed
  info     - in Loxo with no jobs: someone may already be working it, allowed

Blocked companies never appear in outreach queues, can't be enrolled in
follow-ups, aren't pushed to Loxo, and sending to them needs an explicit
override. Companies are matched to Loxo by website domain, then by name.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, timedelta

from .db import DB, now


@dataclass
class Status:
    level: str | None = None   # "blocked" | "warn" | "info" | None
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return self.level == "blocked"


def months() -> int:
    try:
        return int(os.environ.get("OFFLIMITS_MONTHS") or 12)
    except ValueError:
        return 12


def loxo_match(db: DB, company_id: int):
    """The matching Loxo company row, by domain then by normalised name."""
    from .orgmap import norm_company
    co = db.company_row(company_id)
    if not co:
        return None
    if co["domain"]:
        row = db.conn.execute("SELECT * FROM loxo_companies WHERE domain = ? ORDER BY open_jobs DESC LIMIT 1",
                              (co["domain"],)).fetchone()
        if row:
            return row
    name = norm_company(co["name"] or "")
    if len(name) >= 3:
        return db.conn.execute("SELECT * FROM loxo_companies WHERE name_norm = ? ORDER BY open_jobs DESC LIMIT 1",
                               (name,)).fetchone()
    return None


def status(db: DB, company_id: int | None, today: date | None = None) -> Status:
    if not company_id:
        return Status()
    today = today or date.today()
    co = db.company_row(company_id)
    if not co:
        return Status()
    if co["offlimits_reason"]:
        until = co["offlimits_until"]
        if not until or until >= today.isoformat():
            return Status("blocked", f"Off-limits: {co['offlimits_reason']}" + (f" (until {until})" if until else ""))
    lx = loxo_match(db, company_id)
    if not lx:
        return Status()
    owner = f" (owner: {lx['owner']})" if lx["owner"] else ""
    if lx["blocked"] and (not lx["blocked_until"] or lx["blocked_until"] >= today.isoformat()):
        return Status("blocked", "Blocked in Loxo" + (f" until {lx['blocked_until']}" if lx["blocked_until"] else "") + owner)
    status_name = (lx["status_name"] or "").strip()
    if re.search(r"\bclient\b", status_name, re.I) and not re.search(r"former|past|prospective|lost", status_name, re.I):
        return Status("blocked", f"{status_name} in Loxo{owner}")
    if lx["open_jobs"]:
        n = lx["open_jobs"]
        return Status("blocked", f"Active client in Loxo ({n} open job{'s' if n != 1 else ''})")
    cutoff = (today - timedelta(days=round(months() * 30.44))).isoformat()
    if lx["last_placement"] and lx["last_placement"] >= cutoff:
        return Status("blocked", f"Placed into on {lx['last_placement']} (off-limits for {months()} months)")
    if re.search(r"opportunit|pursuing|in progress|negotiat", status_name, re.I):
        return Status("warn", f"{status_name} in Loxo: someone is working this account{owner}")
    if lx["all_jobs"] or lx["placements"]:
        return Status("warn", f"Past client in Loxo: warm re-engagement{owner}")
    return Status("info", f"Already in Loxo{owner}")


def set_manual(db: DB, company_id: int, reason: str | None, until: str | None = None) -> None:
    """Mark (reason) or clear (None) a company as off-limits in Zoomollo."""
    db.update_company(company_id, offlimits_reason=(reason or None), offlimits_until=(until or None))
    db.conn.execute("INSERT INTO activities (company_id, kind, body, created_at) VALUES (?, 'system', ?, ?)",
                    (company_id, f"Marked off-limits: {reason}" if reason else "Off-limits cleared", now()))
    db.commit()


def blocked_ids(db: DB) -> set[int]:
    return {r["id"] for r in db.conn.execute("SELECT id FROM companies") if status(db, r["id"]).blocked}
