"""Re-check companies on a schedule and report what changed.

Every RECHECK_DAYS days (default 7) each company's website and job boards are
looked at again. Differences are recorded as events (db.events):
  job_new       a role that wasn't there last time
  job_reposted  a role that had closed is listed again (often hard to fill)
  job_gone      a role missing two checks in a row (filled or closed)
  person_new    a manager-level or more senior person appears on their site
The first look at a company is the baseline: nothing is reported for it.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

from .db import DB

KINDS = {
    "job_reposted": "Reposted roles",
    "job_new": "New roles",
    "person_new": "New leaders",
    "job_gone": "Roles closed or filled",
}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def every_days() -> int:
    return _int_env("RECHECK_DAYS", 7)


def per_hour() -> int:
    return _int_env("RECHECK_PER_HOUR", 20)


def due(db: DB, limit: int = 20, days: int | None = None) -> list:
    """Companies with a website not checked for `days`: pipeline first, then best score."""
    from .hiringscore import score_all
    days = every_days() if days is None else days
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    # A site that failed to load is not due again until its backoff ends, so the same
    # failures don't use up every hourly slot.
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = db.conn.execute(
        "SELECT co.*, (SELECT 1 FROM pipeline pl WHERE pl.company_id = co.id) AS in_pipeline FROM companies co "
        "WHERE co.domain IS NOT NULL AND (co.last_checked IS NULL OR co.last_checked < ?) "
        "AND (co.crawl_backoff_until IS NULL OR co.crawl_backoff_until <= ?)", (cutoff, now_iso)).fetchall()
    if not rows:
        return []
    scores = score_all(db)
    rows = sorted(rows, key=lambda r: (not r["in_pipeline"], -(scores[r["id"]].total if r["id"] in scores else 0),
                                       r["last_checked"] or ""))
    return rows[:limit]


def run(db: DB, limit: int | None = None, days: int | None = None, pages: int = 15, progress=None,
        delay: float = 1.0) -> dict:
    """Re-check the companies that are due. Returns counts of what changed."""
    from .pipeline import crawl_company
    say = progress or (lambda m: None)
    before = db.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
    checked = 0
    for co in due(db, per_hour() if limit is None else limit, days):
        try:
            r = crawl_company(db, co["domain"], max_pages=pages, delay=delay)
        except Exception as e:  # noqa: BLE001 - one bad site shouldn't stop the run
            from .pipeline import note_crawl_result
            note_crawl_result(db, co["id"], False)
            say(f"✗ {co['name'] or co['domain']}: {e}")
            continue
        if r.get("blocked"):
            continue
        if not r["pages"]:  # recorded with a backoff inside crawl_company, so it won't be due again this hour
            why = "timed out" if r.get("gave_up") else "site didn't load"
            say(f"✗ {co['name'] or co['domain']}: {why}, backed off")
            continue
        checked += 1
    counts = dict.fromkeys(KINDS, 0)
    for k, n in db.conn.execute("SELECT kind, COUNT(*) FROM events WHERE id > ? GROUP BY kind", (before,)):
        counts[k] = n
    counts["checked"] = checked
    if checked:
        say(f"✓ Re-checked {checked} companies: {counts['job_new']} new roles, {counts['job_reposted']} reposted, "
            f"{counts['job_gone']} closed, {counts['person_new']} new leaders")
    return counts


def changes(db: DB, days: int = 7, niche_only: bool = False, pipeline_only: bool = False) -> dict[str, list[dict]]:
    """Events from the last `days`, grouped by kind, newest first, with each company's score."""
    from .hiringscore import _location_ok, job_fit, load_desk, score_all
    desk = load_desk(db)
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = db.conn.execute(
        "SELECT e.*, co.name AS company_name, co.key AS company_key, co.city, j.title AS job_title, "
        "j.location AS job_location, j.salary_max, j.url AS job_url, "
        "(SELECT stage FROM pipeline pl WHERE pl.company_id = e.company_id) AS stage "
        "FROM events e JOIN companies co ON co.id = e.company_id LEFT JOIN jobs j ON j.id = e.job_id "
        "WHERE e.created_at >= ? ORDER BY e.created_at DESC, e.id DESC", (since,)).fetchall()
    scores = score_all(db, desk)
    out: dict[str, list[dict]] = {k: [] for k in KINDS}
    for r in rows:
        d = dict(r)
        d["fit"] = bool(d["job_title"]) and job_fit(d["job_title"], d["job_location"], d["city"], d["salary_max"], desk)[0]
        if niche_only and d["job_title"] and not d["fit"]:
            continue
        if d["job_title"] and desk.locations and not _location_ok(d["job_location"], d["city"], desk, d["job_title"]):
            continue  # a role outside your area isn't news for your desk
        if pipeline_only and not d["stage"]:
            continue
        d["score"] = scores.get(d["company_id"])
        out.setdefault(d["kind"], []).append(d)
    for group in out.values():  # best accounts first, then most recent
        group.sort(key=lambda d: (-(d["score"].total if d["score"] else 0), d["created_at"]), reverse=False)
    return out


def recent_signals(db: DB, today: date | None = None) -> dict[int, dict]:
    """Per company: reposted / new roles and new leaders, for the hiring score."""
    today = today or date.today()
    out: dict[int, dict] = {}
    for e in db.conn.execute(
            "SELECT e.company_id, e.kind, e.created_at, j.title FROM events e LEFT JOIN jobs j ON j.id = e.job_id "
            "WHERE e.created_at >= ? AND (j.id IS NULL OR j.closed_at IS NULL)",
            ((today - timedelta(days=60)).isoformat(),)):
        d = out.setdefault(e["company_id"], {"reposted": [], "new": [], "leaders": 0})
        age = (today - date.fromisoformat(e["created_at"][:10])).days
        if e["kind"] == "job_reposted" and e["title"]:
            d["reposted"].append(e["title"])
        elif e["kind"] == "job_new" and e["title"] and age <= 7:
            d["new"].append(e["title"])
        elif e["kind"] == "person_new" and age <= 30:
            d["leaders"] += 1
    return out


__all__ = ["KINDS", "changes", "due", "every_days", "per_hour", "recent_signals", "run"]
