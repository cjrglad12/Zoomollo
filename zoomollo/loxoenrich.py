"""Contact details through Loxo.

Loxo finds emails and phone numbers for the people in it: its Chrome extension
on a LinkedIn profile ("Find contact info"), and its contact-finding agent
inside Loxo. Loxo's public API has no "find contact info" call, so Zoomollo:

1. adds the person to Loxo as a Contact (name, title, company, LinkedIn link),
   by hand with **Add to Loxo & find contact info** (which then opens their
   LinkedIn profile, where the extension is one click), and automatically for
   the people on your best Today cards who are missing an email or a direct
   line (LOXO_AUTO_ADD a day, default 15; 0 turns it off);
2. reads those Loxo records back every half hour and copies in any email or
   phone Loxo now has. Toll-free, customer service and generic numbers and
   inboxes are never taken.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

from .db import DB, now

PULL_EVERY_HOURS = 6     # how often one person's Loxo record is re-read while details are missing
PULL_FOR_DAYS = 21       # stop re-reading a person this long after they were added


def auto_add_allowance() -> int:
    try:
        return max(0, int(os.environ.get("LOXO_AUTO_ADD", "15")))
    except ValueError:
        return 15


def needs_info(p) -> bool:
    good_email = p["email"] and p["email_status"] in ("found", "confirmed")
    return not (good_email and p["phone"])


def _client(client=None):
    from .loxo import LoxoClient, configured
    if client:
        return client
    return LoxoClient() if configured() else None


def add(db: DB, person_id: int, client=None) -> tuple[int | None, str]:
    """Put a Zoomollo person into Loxo as a Contact (or link the record already there)."""
    from .loxosync import Sync
    from .offlimits import status as offlimits_status
    p = db.conn.execute("SELECT company_id, loxo_person_id FROM people WHERE id = ?", (person_id,)).fetchone()
    if not p:
        return None, "contact no longer in Zoomollo"
    if offlimits_status(db, p["company_id"]).blocked:
        return None, "company is off-limits"
    client = _client(client)
    if not client:
        return None, "Loxo isn't connected"
    lid, how = Sync(db, client).find_or_create(person_id)
    if lid:
        db.conn.execute("UPDATE people SET loxo_added_at = COALESCE(loxo_added_at, ?) WHERE id = ?", (now(), person_id))
        db.commit()
    return lid, how


def _values(items) -> list[str]:
    out = []
    for x in items or []:
        v = x.get("value") if isinstance(x, dict) else x
        if v:
            out.append(str(v).strip())
    return out


def pull(db: DB, person_id: int, client=None) -> dict:
    """Copy the email / phone Loxo has for this person into Zoomollo. Returns what was added."""
    from .exclusions import is_excluded_email
    from .extract import normalize_phone
    from .today import dialable
    p = db.conn.execute("SELECT p.*, co.domain FROM people p JOIN companies co ON co.id = p.company_id "
                        "WHERE p.id = ?", (person_id,)).fetchone()
    if not p or not p["loxo_person_id"]:
        return {}
    client = _client(client)
    if not client:
        return {}
    rec = client.get(f"people/{int(p['loxo_person_id'])}")
    rec = rec.get("person", rec) if isinstance(rec, dict) else {}
    db.conn.execute("UPDATE people SET loxo_pulled_at = ? WHERE id = ?", (now(), person_id))
    got = {}
    emails = [e.lower() for e in _values(rec.get("emails")) + _values([rec.get("email")])
              if "@" in e and not is_excluded_email(e.lower()) and not db.is_suppressed(e.lower())
              and not db.is_bad_email(e.lower())]
    domain = (p["domain"] or "").lower()
    work = [e for e in emails if domain and (e.endswith("@" + domain) or e.endswith("." + domain))]
    email = (work or emails or [None])[0]
    if email and (not p["email"] or p["email_status"] not in ("found", "confirmed")) and email != (p["email"] or ""):
        db.conn.execute("UPDATE people SET email = ?, email_status = 'found' WHERE id = ?", (email, person_id))
        db.add_contact(p["company_id"], "email", email, "person", name=p["name"], title=p["title"],
                       source_url="loxo")
        got["email"] = email
    if not p["phone"]:
        for raw in _values(rec.get("phones")) + _values([rec.get("phone")]):
            ph = normalize_phone(raw)
            if ph and dialable(ph) and not db.is_suppressed(ph):
                db.conn.execute("UPDATE people SET phone = ? WHERE id = ?", (ph, person_id))
                got["phone"] = ph
                break
    if got:
        from . import crm
        crm.log(db, p["company_id"], "system", "From Loxo: " + ", ".join(got.values()) + f" ({p['name']})", person_id)
    db.commit()
    return got


def _added_today(db: DB) -> int:
    return db.conn.execute("SELECT COUNT(*) FROM people WHERE loxo_added_at >= ?",
                           (date.today().isoformat(),)).fetchone()[0]


def auto(db: DB, client=None, progress=None, limit: int | None = None) -> dict:
    """Read back people added recently, then add today's best prospects who are missing details."""
    from .today import call_list
    say = progress or (lambda m: None)
    client = _client(client)
    out = {"pulled": 0, "found": 0, "added": 0}
    if not client:
        return out
    since = (datetime.now(timezone.utc) - timedelta(days=PULL_FOR_DAYS)).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(hours=PULL_EVERY_HOURS)).isoformat()
    rows = db.conn.execute("SELECT * FROM people WHERE loxo_person_id IS NOT NULL AND loxo_added_at >= ? "
                           "AND (loxo_pulled_at IS NULL OR loxo_pulled_at < ?) ORDER BY loxo_added_at DESC LIMIT 60",
                           (since, stale)).fetchall()
    for p in rows:
        if not needs_info(p):
            continue
        try:
            got = pull(db, p["id"], client)
        except Exception as e:  # noqa: BLE001 - one bad record must not stop the rest
            say(f"Loxo read-back for {p['name']} skipped: {e}")
            continue
        out["pulled"] += 1
        out["found"] += bool(got)
    room = auto_add_allowance() - _added_today(db)
    if limit is not None:
        room = min(room, limit)
    if room > 0:
        for r in call_list(db, limit=40, with_pitches=False):
            if room <= 0:
                break
            p = r["person"]
            if not p:
                continue
            full = db.conn.execute("SELECT * FROM people WHERE id = ?", (p["id"],)).fetchone()
            if not full or full["loxo_added_at"] or not needs_info(full):
                continue
            try:
                lid, how = add(db, p["id"], client)
            except Exception as e:  # noqa: BLE001
                say(f"Adding {p['name']} to Loxo skipped: {e}")
                continue
            if lid:
                out["added"] += 1
                room -= 1
    if out["added"] or out["found"]:
        say(f"Loxo contact info: {out['added']} added to Loxo, details found for {out['found']}")
    return out
