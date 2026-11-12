"""Contact details through Loxo.

Loxo finds emails and phone numbers for the people in it: its Chrome extension
on a LinkedIn profile ("Find contact info"), and its contact-finding agent
inside Loxo. Loxo's public API has no "find contact info" call, so Zoomollo:

1. adds people to Loxo as Contacts (name, title, company, LinkedIn link), by
   hand with **Add to Loxo & find contact info** (which then opens their
   LinkedIn profile, where the extension is one click), and automatically for
   your best Today cards: the person on the card and up to two colleagues who
   could also own the hire (LOXO_AUTO_ADD people a day, default 15; 0 = off);
2. reads those Loxo records back every half hour and keeps every email and
   phone Loxo has (mobile, work, direct, personal...), labelled, so the card
   offers each of them. Generic inboxes, toll-free and customer service
   numbers are never taken.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

from .db import DB, now

PULL_EVERY_HOURS = 6     # how often one person's Loxo record is re-read while details are missing
PULL_FOR_DAYS = 21       # stop re-reading a person this long after they were added
PER_COMPANY = 3          # people per account added automatically: the card's contact and two more


def auto_add_allowance() -> int:
    try:
        return max(0, int(os.environ.get("LOXO_AUTO_ADD", "15")))
    except ValueError:
        return 15


def needs_info(p) -> bool:
    """Missing a verified email or a direct line (a phone found on the company site may be
    the front desk, so a person with only that still counts)."""
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


def _label(x: dict, kind: str) -> str | None:
    """'Mobile', 'Work', 'Personal'... from a Loxo email / phone entry, however it's shaped."""
    for k in (f"{kind}_type", "type", "label", "kind", "category"):
        v = x.get(k)
        if isinstance(v, dict):
            v = v.get("name") or v.get("label")
        if v:
            return str(v).strip().title()[:20]
    return None


def _entries(items, kind: str) -> list[tuple[str, str | None]]:
    out = []
    for x in items or []:
        if isinstance(x, dict):
            if x.get("value"):
                out.append((str(x["value"]).strip(), _label(x, kind)))
        elif x:
            out.append((str(x).strip(), None))
    return out


def channels(db: DB, person_id: int) -> list[dict]:
    """Every email and phone known for a person: the main ones first, then the rest."""
    p = db.conn.execute("SELECT email, email_status, phone FROM people WHERE id = ?", (person_id,)).fetchone()
    if not p:
        return []
    rows = [dict(r) for r in db.conn.execute(
        "SELECT type, value, label, source FROM person_channels WHERE person_id = ? ORDER BY id", (person_id,))]
    mine = {r["value"]: r for r in rows}
    main = []
    if p["phone"]:
        main.append(mine.get(p["phone"]) or {"type": "phone", "value": p["phone"], "label": None, "source": "site"})
    if p["email"]:
        main.append(mine.get(p["email"]) or {"type": "email", "value": p["email"], "source": "site",
                                             "label": "guessed" if p["email_status"] == "guessed" else None})
    rows = main + sorted(rows, key=lambda r: r["type"] != "phone")
    seen, out = set(), []
    for r in rows:
        if r["value"] not in seen and not db.is_suppressed(r["value"]):
            seen.add(r["value"])
            out.append(r)
    return out


def pull(db: DB, person_id: int, client=None) -> dict:
    """Copy every email and phone Loxo has for this person into Zoomollo. Returns the new ones.
    The best of each also becomes the person's main email / phone if they had none."""
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
    have = {r["value"] for r in db.conn.execute("SELECT value FROM person_channels WHERE person_id = ?", (person_id,))}
    new: dict[str, str] = {}
    emails = []
    for e, label in _entries(rec.get("emails"), "email") + _entries([rec.get("email")], "email"):
        e = e.lower()
        if "@" in e and not is_excluded_email(e) and not db.is_suppressed(e) and not db.is_bad_email(e):
            emails.append((e, label))
    phones = []
    for raw, label in _entries(rec.get("phones"), "phone") + _entries([rec.get("phone")], "phone"):
        ph = normalize_phone(raw)
        if ph and dialable(ph) and not db.is_suppressed(ph):
            phones.append((ph, label))
    t = now()
    for kind, items in (("email", emails), ("phone", phones)):
        for value, label in items:
            if value not in have:
                db.conn.execute("INSERT OR IGNORE INTO person_channels (person_id, type, value, label, source, added_at) "
                                "VALUES (?, ?, ?, ?, 'loxo', ?)", (person_id, kind, value, label, t))
                have.add(value)
                new[value] = kind
    domain = (p["domain"] or "").lower()
    work = [e for e, _ in emails if domain and (e.endswith("@" + domain) or e.endswith("." + domain))]
    email = (work or [e for e, _ in emails] or [None])[0]
    if email and (not p["email"] or p["email_status"] not in ("found", "confirmed")) and email != (p["email"] or ""):
        db.conn.execute("UPDATE people SET email = ?, email_status = 'found' WHERE id = ?", (email, person_id))
        db.add_contact(p["company_id"], "email", email, "person", name=p["name"], title=p["title"], source_url="loxo")
    if phones and not p["phone"]:
        # A mobile or direct line first: they reach the person, not a desk.
        best = sorted(phones, key=lambda x: 0 if (x[1] or "").lower() in ("mobile", "cell", "direct") else 1)[0][0]
        db.conn.execute("UPDATE people SET phone = ? WHERE id = ?", (best, person_id))
    if new:
        from . import crm
        crm.log(db, p["company_id"], "system", f"From Loxo for {p['name']}: " + ", ".join(new), person_id)
    db.commit()
    return new


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
        # Best cards first: the person on the card, then their next-best colleagues, so each
        # account ends up with a few ways in.
        cards = call_list(db, limit=40, with_pitches=False)
        wanted = [[r["person"]] + r["others"][:PER_COMPANY - 1] for r in cards if r["person"]]
        for rank in range(PER_COMPANY):
            for people in wanted:
                if room <= 0 or rank >= len(people):
                    continue
                p = people[rank]
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
