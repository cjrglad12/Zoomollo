"""Scoreboard: how did I do this week?

Counted from what you log in Zoomollo (Today's outcome buttons, pitch emails, texts,
follow-up emails and pipeline stage changes). Weeks run Monday to Sunday in this
computer's time zone. Goals are yours to set on the Scoreboard page.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from .db import DB

DEFAULT_GOALS = {"daily_dials": 20, "weekly_connects": 15, "weekly_pitches": 10, "weekly_meetings": 2}
GOAL_LABELS = {"daily_dials": "Dials per day", "weekly_connects": "Conversations per week",
               "weekly_pitches": "Candidate pitches per week", "weekly_meetings": "Meetings booked per week"}


def load_goals(db: DB) -> dict:
    try:
        saved = json.loads(db.get_setting("goals") or "{}")
    except ValueError:
        saved = {}
    return {k: int(saved.get(k) or v) for k, v in DEFAULT_GOALS.items()}


def save_goals(db: DB, values: dict) -> dict:
    goals = load_goals(db)
    for k in DEFAULT_GOALS:
        try:
            n = int(str(values.get(k, "")).strip())
            if 0 < n < 10_000:
                goals[k] = n
        except ValueError:
            pass
    db.set_setting("goals", json.dumps(goals))
    db.commit()
    return goals


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())  # Monday


def _local(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts).astimezone()
    except (TypeError, ValueError):
        return None


def _classify(kind: str, body: str) -> list[str]:
    """What one logged activity counts as."""
    b = (body or "").lower()
    if kind == "call":
        out = ["dials"]
        if b.startswith(("call: connected", "meeting booked", "not interested")):
            out.append("connects")
        if b.startswith("call: left voicemail"):
            out.append("voicemails")
        if b.startswith("meeting booked"):
            out.append("meetings")
        return out
    if kind == "email":
        return ["emails"]
    if kind == "pitch":
        return ["pitches"]
    if kind == "text":
        return ["texts"]
    if kind == "stage" and b.startswith("stage: meeting"):
        return ["meetings"]
    return []


METRICS = ["dials", "connects", "voicemails", "meetings", "pitches", "emails", "texts", "accounts"]


def tally(db: DB, start: date, end: date) -> dict:
    """Totals between two local dates (inclusive), plus a per-day breakdown and accounts touched."""
    rows = db.conn.execute(
        "SELECT company_id, kind, body, created_at FROM activities WHERE created_at >= ? AND created_at < ? "
        "ORDER BY created_at", ((start - timedelta(days=1)).isoformat(), (end + timedelta(days=2)).isoformat()))
    totals = {m: 0 for m in METRICS}
    by_day = {start + timedelta(days=i): {m: 0 for m in METRICS} for i in range((end - start).days + 1)}
    touched: set[int] = set()
    meeting_cos: set[tuple] = set()  # a meeting logged on Today also changes the stage: count it once
    for r in rows:
        t = _local(r["created_at"])
        if not t or not (start <= t.date() <= end):
            continue
        for m in _classify(r["kind"], r["body"]):
            if m == "meetings":
                if (r["company_id"], t.date()) in meeting_cos:
                    continue
                meeting_cos.add((r["company_id"], t.date()))
            totals[m] += 1
            by_day[t.date()][m] += 1
        if r["kind"] in ("call", "email", "pitch", "text") and r["company_id"]:
            touched.add(r["company_id"])
    totals["accounts"] = len(touched)
    return {"totals": totals, "by_day": by_day}


def _rate(a: int, b: int) -> str:
    return f"{round(100 * a / b)}%" if b else "–"


def week(db: DB, today: date | None = None) -> dict:
    """This week vs last week, goals and the per-day dial chart."""
    today = today or date.today()
    ws = week_start(today)
    this = tally(db, ws, ws + timedelta(days=6))
    last = tally(db, ws - timedelta(days=7), ws - timedelta(days=1))
    goals = load_goals(db)
    workdays_so_far = sum(1 for i in range((today - ws).days + 1) if (ws + timedelta(days=i)).weekday() < 5)
    t = this["totals"]
    progress = [
        ("Dials", t["dials"], goals["daily_dials"] * 5, f"{goals['daily_dials']} a day"),
        ("Conversations", t["connects"], goals["weekly_connects"], "people you spoke to"),
        ("Candidate pitches", t["pitches"], goals["weekly_pitches"], "pitch emails sent"),
        ("Meetings booked", t["meetings"], goals["weekly_meetings"], "the number that pays"),
    ]
    days = [{"date": d, "label": d.strftime("%a"), "dials": v["dials"], "connects": v["connects"],
             "is_today": d == today} for d, v in this["by_day"].items() if d.weekday() < 5]
    on_pace = goals["daily_dials"] * workdays_so_far
    return {"start": ws, "end": ws + timedelta(days=6), "this": t, "last": last["totals"], "goals": goals,
            "progress": progress, "days": days, "max_day": max([d["dials"] for d in days] + [goals["daily_dials"]]),
            "pace": {"expected": on_pace, "actual": t["dials"]},
            "rates": {"connect": _rate(t["connects"], t["dials"]), "meeting": _rate(t["meetings"], t["connects"]),
                      "connect_last": _rate(last["totals"]["connects"], last["totals"]["dials"]),
                      "meeting_last": _rate(last["totals"]["meetings"], last["totals"]["connects"])}}


def today_progress(db: DB, today: date | None = None) -> dict:
    """Dials logged today against the daily goal (for the bar on Today)."""
    today = today or date.today()
    d = tally(db, today, today)["totals"]
    goal = load_goals(db)["daily_dials"]
    return {"dials": d["dials"], "goal": goal, "connects": d["connects"], "meetings": d["meetings"],
            "pct": min(100, round(100 * d["dials"] / goal)) if goal else 0}
