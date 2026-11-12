"""Hourly auto-prospecting: email the best accounts that are actively hiring, a few each hour.

Each hourly run (weekdays, inside your sending hours) sends at most `per_hour` emails:
due follow-ups for people it started first, then new prospects, best hiring score first.
New prospects are started on a follow-up sequence, so replies, unsubscribes and bounces
are handled like any other sequence. It pauses itself if too many addresses bounce.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

from . import crm
from .db import DB, now
from .extract import plausible_title

SETTING = "autosend"
DEFAULTS = {"on": False, "per_hour": 5, "start": 8, "end": 16, "weekdays": True, "sequence_id": None}
RECENT_EMAIL_DAYS = 21    # an account emailed this recently is left alone
TEXT_WAIT_DAYS = 3        # an unsent text step waits this long, then is skipped
BOUNCE_WINDOW_DAYS = 7
BOUNCE_MIN_SENT = 8       # judge the bounce rate only after this many sends
BOUNCE_MAX_RATE = 0.25    # pause above this share of bounces


def settings(db: DB) -> dict:
    try:
        saved = json.loads(db.get_setting(SETTING) or "{}")
    except ValueError:
        saved = {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def save_settings(db: DB, **changes) -> dict:
    cfg = {**settings(db), **{k: v for k, v in changes.items() if k in DEFAULTS}}
    cfg["per_hour"] = max(1, min(int(cfg["per_hour"]), 20))
    cfg["start"] = max(0, min(int(cfg["start"]), 23))
    cfg["end"] = max(cfg["start"] + 1, min(int(cfg["end"]), 24))
    db.set_setting(SETTING, json.dumps(cfg))
    return cfg


def in_window(cfg: dict, at: datetime | None = None) -> bool:
    at = at or datetime.now()
    if cfg["weekdays"] and at.weekday() >= 5:
        return False
    return cfg["start"] <= at.hour < cfg["end"]


def sequence_id(db: DB, cfg: dict | None = None) -> int | None:
    """The sequence new prospects start on: the chosen one, else the first that opens with an email."""
    cfg = cfg or settings(db)
    seqs = crm.sequences(db)
    for s in seqs:
        if s["id"] == cfg.get("sequence_id") and s["steps"] and s["steps"][0]["channel"] == "email":
            return s["id"]
    for s in seqs:
        if s["steps"] and s["steps"][0]["channel"] == "email" and s["steps"][0]["template_id"]:
            return s["id"]
    return None


def blockers(db: DB) -> list[str]:
    """What has to be fixed before anything can go out on its own."""
    out = []
    if not crm.smtp_configured():
        out.append("Connect Gmail (Pipeline → Email) so Zoomollo can send.")
    if not crm.imap_configured():
        out.append("Inbox checking must be on so replies and bounces stop follow-ups.")
    if not (db.get_setting("my_address") or os.environ.get("MY_ADDRESS")):
        out.append("Add your business mailing address (Messages → Your details); sales email needs it.")
    if not sequence_id(db):
        out.append("Make a follow-up sequence whose first step is an email.")
    return out


def bounce_problem(db: DB) -> str | None:
    """Too many guessed addresses bouncing hurts your Gmail reputation: say so, so sending pauses."""
    since = (date.today() - timedelta(days=BOUNCE_WINDOW_DAYS)).isoformat()
    rows = db.conn.execute(
        "SELECT DISTINCT em.to_addr, b.value IS NOT NULL AS bounced FROM emails em "
        "JOIN enrollments n ON n.id = em.enrollment_id LEFT JOIN bad_emails b ON b.value = em.to_addr "
        "WHERE n.auto = 1 AND em.method = 'smtp' AND em.sent_at >= ?", (since,)).fetchall()
    sent, bounced = len(rows), sum(r["bounced"] for r in rows)
    if sent >= BOUNCE_MIN_SENT and bounced / sent > BOUNCE_MAX_RATE:
        return (f"Auto-send paused: {bounced} of the last {sent} addresses bounced. Too many bounces can get "
                f"your Gmail flagged. Check the guessed emails, then turn it back on.")
    return None


def _recently_emailed_companies(db: DB) -> set[int]:
    since = (date.today() - timedelta(days=RECENT_EMAIL_DAYS)).isoformat()
    ids = {r[0] for r in db.conn.execute("SELECT DISTINCT company_id FROM emails WHERE company_id IS NOT NULL "
                                         "AND sent_at >= ?", (since,))}
    ids |= {r[0] for r in db.conn.execute("SELECT DISTINCT company_id FROM enrollments WHERE status = 'active'")}
    return ids


def _reachable(db: DB, p, emailed: set[str], enrolled: set[int]) -> bool:
    e = (p["email"] or "").strip().lower()
    return bool(e and "@" in e and p["email_status"] != "failed" and p["id"] not in enrolled
                and e not in emailed and not db.is_suppressed(e) and not db.is_bad_email(e)
                and (not p["title"] or plausible_title(p["title"])))


def in_area(db: DB, company_id: int, desk) -> bool:
    """Is this account confirmed local? A fitting role that states a city in your area, or the
    company's own address there. A careers page with no locations at a company with no known
    city could be anywhere, and an unasked email there is wasted."""
    from .hiringscore import _location_ok, job_fit
    if not desk.locations:
        return True
    co = db.company_row(company_id)
    if co and (co["city"] or "").strip() and _location_ok(None, co["city"], desk):
        return True
    for j in db.conn.execute("SELECT title, location, salary_max FROM jobs WHERE company_id = ? AND closed_at IS NULL "
                             "AND location IS NOT NULL AND trim(location) <> ''", (company_id,)):
        if job_fit(j["title"], j["location"], None, j["salary_max"], desk)[0]:
            return True
    return False


def pick_prospects(db: DB, n: int) -> list[dict]:
    """The best n people to email: one per account, highest hiring score first."""
    from .hiringscore import load_desk, score_all
    from .orgmap import SENIORITY_RANK
    from .today import QUIET_DAYS, SKIP_STAGES, _business_days_ago, _local, _recent_touch, pick_person
    if n <= 0:
        return []
    desk = load_desk(db)
    scores = score_all(db, desk)
    stages = {r["company_id"]: r["stage"] for r in db.conn.execute("SELECT company_id, stage FROM pipeline")}
    skip = _recently_emailed_companies(db)
    emailed = {r[0] for r in db.conn.execute("SELECT DISTINCT lower(to_addr) FROM emails")}
    enrolled = {r[0] for r in db.conn.execute("SELECT DISTINCT person_id FROM enrollments")}
    names = {r["id"]: r["name"] or r["key"] for r in db.conn.execute("SELECT id, name, key FROM companies")}
    out = []
    for cid, s in sorted(scores.items(), key=lambda kv: -kv[1].total):
        if len(out) >= n:
            break
        if s.total <= 0 or not s.fit_roles or (s.offlimits and s.offlimits.blocked):
            continue
        if cid in skip or stages.get(cid) in SKIP_STAGES:
            continue
        recent = _recent_touch(db, cid, date.today())
        t = _local(recent) if recent else None
        if t and _business_days_ago(t.date(), date.today()) < QUIET_DAYS:
            continue  # you called or texted them in the last few days
        if not in_area(db, cid, desk):
            continue  # no role that states a city in your area, and no local address for the company
        people = [p for p in db.conn.execute("SELECT * FROM people WHERE company_id = ?", (cid,))
                  if _reachable(db, p, emailed, enrolled)]
        person, why = pick_person(people, s.fit_roles)
        if not person:  # nobody obvious: the most senior reachable leader
            # Directors and up only: a manager from an unrelated team (say, content marketing for a
            # Controller opening) is the wrong inbox, and a guessed address makes it worse.
            top = SENIORITY_RANK.get("director", 3)
            leaders = [p for p in people if SENIORITY_RANK.get(p["seniority"] or "staff", 1) >= top]
            if not leaders:
                continue
            person = max(leaders, key=lambda p: SENIORITY_RANK.get(p["seniority"] or "staff", 1))
            why = "senior leader"
        out.append({"person_id": person["id"], "name": person["name"], "title": person["title"],
                    "email": person["email"], "email_status": person["email_status"], "company_id": cid,
                    "company": names.get(cid), "score": round(s.total), "why": why,
                    "role": s.fit_roles[0]})
    return out


def _send(db: DB, it: dict, say) -> bool:
    try:
        mid = crm.send_smtp(db, it["to"], it["subject"], it["body"])
    except Exception as ex:  # noqa: BLE001 - report and carry on
        say(f"✗ Auto-send {it['to']}: {ex}")
        return False
    crm.complete_step(db, it["enrollment_id"], "smtp", it["subject"], it["body"], it["to"])
    db.conn.execute("UPDATE emails SET message_id = ? WHERE id = (SELECT MAX(id) FROM emails)", (mid,))
    db.commit()
    say(f"✓ Auto-sent: {it['name']} ({it.get('company_name') or ''}) <{it['to']}>")
    return True


def _due_auto(db: DB, only: set[int] | None = None) -> list[dict]:
    auto = {r[0] for r in db.conn.execute("SELECT id FROM enrollments WHERE auto = 1 AND status = 'active'")}
    return [it for it in crm.due_items(db) if it["kind"] == "step" and it["channel"] == "email"
            and it["enrollment_id"] in auto and (only is None or it["enrollment_id"] in only)]


def skip_stale_texts(db: DB) -> int:
    """A text step you haven't sent after a few days is skipped, so the follow-up emails
    behind it still go out. Only for people auto-send started."""
    cutoff = (date.today() - timedelta(days=TEXT_WAIT_DAYS)).isoformat()
    rows = db.conn.execute(
        "SELECT e.id, e.person_id, e.company_id FROM enrollments e JOIN sequence_steps s "
        "ON s.sequence_id = e.sequence_id AND s.step_no = e.step_no "
        "WHERE e.auto = 1 AND e.status = 'active' AND s.channel != 'email' AND e.next_due <= ?", (cutoff,)).fetchall()
    for r in rows:
        crm.skip_step(db, r["id"])
        crm.log(db, r["company_id"], "system",
                f"Text step skipped (not sent in {TEXT_WAIT_DAYS} days) so follow-up emails continue", r["person_id"])
    db.commit()
    return len(rows)


def run_hour(db: DB, progress=None, force: bool = False, pause: float = 8.0) -> dict:
    """One hourly run. Returns {'sent', 'followups', 'new', 'note'}."""
    import time
    say = progress or (lambda m: None)
    cfg = settings(db)
    res = {"sent": 0, "followups": 0, "new": 0, "note": ""}
    if not cfg["on"] and not force:
        return res
    problems = blockers(db)
    if problems:
        res["note"] = "Auto-send waiting: " + " ".join(problems)
        say("✗ " + res["note"])
        return res
    bad = bounce_problem(db)
    if bad:
        save_settings(db, on=False)
        db.set_setting("autosend_note", bad)
        res["note"] = bad
        say("✗ " + bad)
        return res
    budget = min(int(cfg["per_hour"]), crm.daily_limit() - crm.sent_today(db))
    if budget <= 0:
        res["note"] = f"Daily email limit reached ({crm.daily_limit()})."
        return res
    # 1. Follow-ups that are due for people auto-send started.
    skip_stale_texts(db)
    for it in _due_auto(db):
        if res["sent"] >= budget:
            break
        if _send(db, it, say):
            res["sent"] += 1
            res["followups"] += 1
            if pause:
                time.sleep(pause)
    # 2. New prospects, best first.
    seq = sequence_id(db, cfg)
    for p in pick_prospects(db, budget - res["sent"]):
        try:
            eid = crm.enroll(db, p["person_id"], seq)
        except Exception as ex:  # noqa: BLE001 - off-limits, no email, ...
            say(f"✗ Auto-send skipped {p['name']}: {ex}")
            continue
        db.conn.execute("UPDATE enrollments SET auto = 1 WHERE id = ?", (eid,))
        db.commit()
        items = _due_auto(db, {eid})
        if items and _send(db, items[0], say):
            res["sent"] += 1
            res["new"] += 1
            if pause:
                time.sleep(pause)
    db.set_setting("autosend_last", now())
    if not res["sent"]:
        res["note"] = "No new hiring prospects with an email address right now."
    db.set_setting("autosend_note", res["note"] or f"Last run sent {res['sent']}.")
    return res
