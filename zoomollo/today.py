"""Today: who to call, why now, and what to say.

Built from what Zoomollo already knows: the hiring score (roles that fit your desk,
in your area), the people found at each company, contact details, pipeline stage
and your logged activity. Nothing is fetched from the internet.

Each account appears once, with the single best person to call:
  1. the likely hiring manager for a role that fits your desk,
  2. else the most senior person in that role's department (director / VP first),
  3. else an HR / talent leader, 4. else an executive.
Accounts you logged a call or email to recently drop off until they're due again.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from .db import DB
from .orgmap import SENIORITY_RANK, department

QUIET_DAYS = 3            # after a logged touch, an account rests this many business days
SKIP_STAGES = {"replied", "meeting", "agreement", "job_order", "placement", "lost", "not_now"}
OUTCOMES = {
    "connected": "Call: connected",
    "voicemail": "Call: left voicemail",
    "no_answer": "Call: no answer",
    "emailed": "Emailed",
    "meeting": "Meeting booked",
    "not_interested": "Not interested",
    "bad_number": "Bad number",
}
_TOLL_FREE = ("800", "833", "844", "855", "866", "877", "888")
_DFW_CODES = ("214", "469", "972", "945", "817", "682", "940")
TOUCH_KINDS = ("call", "email", "text")


def _business_days_ago(d: date, today: date) -> int:
    n, cur = 0, d
    while cur < today:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


def _add_business_days(start: date, n: int) -> date:
    cur = start
    while n > 0:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n -= 1
    return cur


_DFW_PLACES = re.compile(r"dallas|fort-?worth|ft-?worth|plano|irving|frisco|richardson|addison|arlington|southlake|"
                         r"westlake|mckinney|allen|carrollton|lewisville|grapevine|coppell|denton|garland|keller|"
                         r"las-?colinas|dfw|north-?texas|headquarters|\bhq\b", re.I)
_OFFICE_PAGE = re.compile(r"/(locations?|offices?)/([^/]+)", re.I)
_CONTACT_PAGE = re.compile(r"/(contact|contact-us|about|about-us|locations?|offices?)(/|$)|^/?$", re.I)


def _area(number: str) -> str:
    return number[2:5] if (number or "").startswith("+1") else ""


def line_note(number: str | None) -> str:
    """What to know before dialing: '', 'toll-free' or 'not a DFW number'."""
    n = re.sub(r"[^\d+]", "", number or "")
    if not n:
        return ""
    if not n.startswith("+"):
        n = "+1" + n[-10:]
    if _area(n) in _DFW_CODES:
        return ""
    return "toll-free" if _area(n) in _TOLL_FREE else "not a DFW number"


def dialable(number: str | None) -> bool:
    """Worth dialing. Toll-free numbers (800, 888...) go to customer service or a phone
    tree, so they count as no number at all."""
    n = re.sub(r"[^\d+]", "", number or "")
    if not n:
        return False
    if not n.startswith("+"):
        n = "+1" + n[-10:]
    return _area(n) not in _TOLL_FREE


def open_roles(db: DB, company_id: int, fit_roles: list[str], today: date | None = None) -> list[dict]:
    """The company's open roles for a card: the ones on your desk first, then the rest.
    The same title in several places is listed once ('x3')."""
    today = today or date.today()
    fit = set(fit_roles)
    rows = db.conn.execute("SELECT title, url, location, posted_at, first_seen FROM jobs "
                           "WHERE company_id = ? AND closed_at IS NULL AND title IS NOT NULL AND title != '' "
                           "ORDER BY COALESCE(posted_at, first_seen) DESC", (company_id,)).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        name = clean_role(r["title"])
        key = name.lower()
        if key in out:
            out[key]["n"] += 1
            if r["title"] in fit:
                out[key]["fit"] = True
            continue
        age = None
        try:
            days = (today - date.fromisoformat((r["posted_at"] or r["first_seen"] or "")[:10])).days
            age = "new today" if days <= 0 else f"{days}d ago" if days < 60 else f"{days // 30}mo ago"
        except ValueError:
            pass
        out[key] = {"title": name, "url": r["url"], "location": r["location"], "age": age,
                    "fit": r["title"] in fit, "n": 1}
    return sorted(out.values(), key=lambda j: not j["fit"])


def _line_rank(row) -> tuple:
    """Lower is better: a DFW office number from a contact page first; another city's
    office, or a number that only appeared in a news post or advisory, last."""
    from urllib.parse import urlparse
    from .pipeline import article_page
    number, path = row["value"], urlparse(row["source_url"] or "").path
    office = _OFFICE_PAGE.search(path)
    elsewhere = bool(office and not _DFW_PLACES.search(office.group(2)))
    area = _area(number)
    return (0 if number.startswith("+1") else 1,
            0 if area in _DFW_CODES else 1,
            1 if elsewhere else 0,                      # another city's office page
            1 if article_page(row["source_url"] or "") else 0,
            0 if area in _TOLL_FREE else 1,             # no DFW line: the national line beats a far-away office
            0 if _CONTACT_PAGE.search(path) else 1,
            {"company": 0, "role": 1}.get(row["kind"], 2), row["id"])


def _main_line(db: DB, company_id: int, listed: str | None) -> str | None:
    if listed and dialable(listed) and not db.is_suppressed(listed):
        return listed
    rows = db.conn.execute("SELECT id, value, kind, source_url FROM contacts WHERE company_id = ? AND type = 'phone'",
                           (company_id,)).fetchall()
    # An overseas office is no use to a DFW desk; a toll-free line or a number read from a
    # help / support page goes to customer service.
    # A number that only appeared in a news post or advisory is rarely theirs.
    from .exclusions import is_support_page
    from .pipeline import article_page
    rows = [r for r in rows if r["value"].startswith("+1") and dialable(r["value"])
            and not is_support_page(r["source_url"]) and not article_page(r["source_url"] or "")]
    return min(rows, key=_line_rank)["value"] if rows else None


def retire_number(db: DB, company_id: int, person_id: int | None = None, note: str = "") -> dict | None:
    """The number on the card reaches nobody useful (tech support, disconnected, wrong company):
    remove it and keep it from coming back when the site is read again. Returns what was removed."""
    from . import crm
    co = db.company_row(company_id)
    p = db.conn.execute("SELECT id, phone FROM people WHERE id = ?", (person_id,)).fetchone() if person_id else None
    number = (p["phone"] if p and p["phone"] else None) or _main_line(db, company_id, co["phone"] if co else None)
    if not number:
        return None
    was = {"number": number, "company_phone": bool(co and co["phone"] == number),
           "people": [r[0] for r in db.conn.execute("SELECT id FROM people WHERE phone = ?", (number,))],
           "contacts": [dict(r) for r in db.conn.execute("SELECT * FROM contacts WHERE value = ?", (number,))]}
    db.suppress(number, "bad number" + (f": {note.strip()}" if note.strip() else ""))
    if was["company_phone"]:
        db.update_company(company_id, phone=None)
    crm.log(db, company_id, "system", f"Bad number removed: {number}" + (f" ({note.strip()})" if note.strip() else ""),
            person_id)
    db.commit()
    return was


def _contact_score(p) -> tuple:
    email_rank = {"confirmed": 3, "found": 2, "guessed": 1}.get(p["email_status"] or "", 0) if p["email"] else 0
    return (1 if p["phone"] else 0, email_rank)


def pick_person(people: list, fit_roles: list[str]):
    """(person, why this person) — the best person to call about these roles."""
    from .exclusions import is_support_title
    people = [p for p in people if not is_support_title(p["title"])]  # CSRs and support reps never hire
    if not people:
        return None, ""
    # Not someone whose title names a different specialty: the Audit partner for tax openings.
    main = main_kind(fit_roles)
    people = [p for p in people if not main or title_kind(p["title"]) in (None, main)] or people
    fit_set = set(fit_roles)
    hms = [p for p in people if p["hiring_for"] and
           fit_set & {t.strip() for t in re.split(r";|\(\+", p["hiring_for"]) if t.strip()}]
    if hms:
        # Among people tied to these roles: a director or VP who leads that kind of work beats a
        # manager from another team who merely has a phone number on file.
        depts = {department(t) for t in fit_roles} - {"other"}
        rank = lambda p: (bool(p["email"] or p["phone"]),
                          2 if p["seniority"] in ("director", "vp") else 1 if p["seniority"] in ("manager", "c_level") else 0,
                          p["department"] in depts, *_contact_score(p))
        return max(hms, key=rank), "likely hiring manager"
    depts = {department(t) for t in fit_roles} - {"other"}
    level = lambda p: SENIORITY_RANK.get(p["seniority"] or "staff", 1)
    # Directors and VPs own hires; C-level at big companies rarely takes the call.
    pref = lambda p: (2 if p["seniority"] in ("director", "vp") else 1 if p["seniority"] == "manager" else 0,
                      *_contact_score(p), level(p))
    same = [p for p in people if p["department"] in depts and level(p) >= 2]
    if same:
        return max(same, key=pref), "leads that team"
    hr = [p for p in people if p["department"] == "hr" and level(p) >= 2]
    if hr:
        return max(hr, key=pref), "talent / HR leader"
    execs = [p for p in people if p["department"] == "executive" or p["seniority"] == "c_level"]
    if execs:
        return max(execs, key=lambda p: (*_contact_score(p), level(p))), "executive"
    return None, ""


def clean_role(title: str) -> str:
    """'AI Software Engineer-Reliability (Contract) (On-site)' -> 'AI Software Engineer'."""
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", title or "")
    t = re.sub(r"[()\[\]]", " ", t)  # stray brackets: "Senior Data Engineer on-site)"
    t = re.split(r"\s+[–—|]\s+|\s+-\s+|-(?=[A-Z][a-z])|,\s*", t)[0]
    t = re.sub(r"\b(on-?site|remote|hybrid|contract|full[- ]time|part[- ]time)\b", " ", t, flags=re.I)
    t = re.sub(r"\s+(\d+|I{1,3}|IV)\s*$", "", t.strip())  # "Software Engineer 2" -> "Software Engineer"
    return re.sub(r"\s+", " ", t).strip(" -,") or (title or "").strip()


def opener(score, person_name: str | None, city: str | None, my_name: str, pitch: dict | None = None,
           my_company: str = "") -> str:
    """A cold-call opener you can say in under 10 seconds: who you are, one reason, a permission ask."""
    first = (person_name or "").split()[0] if person_name else "there"
    role = clean_role(score.fit_roles[0]) if score.fit_roles else "open roles"
    place = (city or "DFW").split(",")[0]
    reasons = " ".join(r for _, r in score.reasons)
    who_me = " ".join(x for x in [my_name.split()[0] if my_name else "", f"with {my_company}" if my_company else ""] if x)
    me = f", {who_me}" if who_me else ""
    a = "an" if role[:1].lower() in "aeiou" else "a"
    ask = "Worth 30 seconds?"
    if not person_name:
        # Calling the main line: get past the front desk to the person who owns the role.
        intro = f"Hi, this is {who_me}." if who_me else "Hi there."
        return f"{intro} Who's the hiring manager for your {role} opening? I have a quick question for them."
    if pitch and pitch.get("local") and not pitch.get("stale"):
        # Strongest opener there is: a real, local candidate for their open role (no name given).
        ct = clean_role(pitch["c"]["title"])
        cplace = (pitch["c"]["city"] or place).split(",")[0]
        if re.search(r"(engineer|developer|manager|accountant|director|analyst|architect|scientist|consultant|"
                     r"specialist|lead|cpa|designer|administrator|officer|controller|auditor|advisor|planner)s?$",
                     ct, re.I):
            who = f"{'an' if ct[:1].lower() in 'aeiou' else 'a'} {ct}"
        else:
            who = f"a candidate with a {ct} background"
        return (f"Hi {first}{me}. I'll be quick: I have {who} in {cplace} who fits your "
                f"{clean_role(pitch['job_title'])} opening. {ask}")
    if "reposted" in reasons or "hard to fill" in reasons:
        return (f"Hi {first}{me}. I'll be quick: your {role} role has been open a while, and I recruit "
                f"for exactly that in {place}. {ask}")
    if "new this week" in reasons:
        return f"Hi {first}{me}. I'll be quick: I saw your new {role} opening. I recruit for that in {place}. {ask}"
    return (f"Hi {first}{me}. I'll be quick: I saw you're hiring {a} {role}, and I recruit for exactly that in "
            f"{place}. {ask}")


def _tidy_reason(reason: str, fit_roles: list[str]) -> str:
    """'4 roles in your niche: AI Software Engineer-Reliability (Contract) (On-site), …' ->
    '4 roles in your niche: AI Software Engineer, DevOps Engineer, Senior Data Engineer'."""
    head, sep, tail = reason.partition(": ")
    if not sep or not any(t in tail for t in fit_roles):
        return reason
    names = list(dict.fromkeys(clean_role(t) for t in fit_roles if t in tail))
    return f"{head}: {', '.join(names)}"


def last_touches(db: DB) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for r in db.conn.execute(
            "SELECT company_id, kind, body, created_at FROM activities WHERE company_id IS NOT NULL "
            "AND kind IN ('call', 'email', 'text', 'note', 'stage') ORDER BY created_at"):
        out[r["company_id"]] = dict(r)
    for r in db.conn.execute("SELECT company_id, sent_at FROM emails WHERE company_id IS NOT NULL ORDER BY sent_at"):
        prev = out.get(r["company_id"])
        if not prev or (r["sent_at"] or "") > prev["created_at"]:
            out[r["company_id"]] = {"kind": "email", "body": "Emailed", "created_at": r["sent_at"]}
    return out


def _recent_touch(db: DB, company_id: int, today: date) -> str | None:
    r = db.conn.execute(
        f"SELECT MAX(created_at) FROM activities WHERE company_id = ? AND kind IN ({','.join('?' * len(TOUCH_KINDS))})",
        (company_id, *TOUCH_KINDS)).fetchone()[0]
    return r


_CACHE: dict = {}


def _signature(db: DB) -> tuple:
    """Changes whenever anything the call list depends on changes."""
    if db.conn.in_transaction:  # unsaved changes of our own: look properly
        return _signature_now(db)
    stamp = db.stamp()
    if stamp[0] is not None and _SIG.get(db.path, (None,))[0] == stamp:
        return _SIG[db.path][1]  # nothing has been written since the last look
    _SIG[db.path] = (stamp, _signature_now(db))
    return _SIG[db.path][1]


_SIG: dict = {}


def _signature_now(db: DB) -> tuple:
    return tuple(db.conn.execute(
        "SELECT (SELECT COALESCE(MAX(id), 0) FROM activities), (SELECT MAX(updated_at) FROM pipeline), "
        "(SELECT COUNT(*) || MAX(COALESCE(last_seen, '')) || COALESCE(SUM(closed_at IS NOT NULL), 0) FROM jobs), "
        "(SELECT COUNT(*) || MAX(id) FROM people), (SELECT COUNT(*) || MAX(COALESCE(resume_fetched_at, '')) FROM candidates), "
        "(SELECT value FROM app_settings WHERE key = 'desk'), (SELECT COUNT(*) FROM enrollments WHERE status = 'active'), "
        "(SELECT COUNT(*) || '/' || COALESCE(SUM(headcount), 0) || '/' || COALESCE(SUM(length(summary)), 0) FROM companies), "
        "(SELECT COUNT(*) || '/' || COALESCE(MAX(found_at), '') FROM leads)").fetchone())


def call_list(db: DB, limit: int = 25, today: date | None = None, with_pitches: bool = True) -> list[dict]:
    """Cached wrapper: the list is only rebuilt when the data behind it changes."""
    if today is not None:
        return _call_list(db, limit, today, with_pitches)
    try:
        key = (db.path, limit, with_pitches, date.today().isoformat(), db.get_setting("my_name"), _signature(db))
    except Exception:  # noqa: BLE001 - never let caching break the page
        return _call_list(db, limit, None, with_pitches)
    if key not in _CACHE:
        if len(_CACHE) > 20:
            _CACHE.clear()
        _CACHE[key] = _call_list(db, limit, None, with_pitches)
    return _CACHE[key]


def _call_list(db: DB, limit: int = 25, today: date | None = None, with_pitches: bool = True) -> list[dict]:
    from .crm import STAGE_NAMES
    from .hiringscore import load_desk, score_all
    today = today or date.today()
    desk = load_desk(db)
    scores = score_all(db, desk)
    companies = {r["id"]: r for r in db.conn.execute("SELECT * FROM companies")}
    stages = {r["company_id"]: dict(r) for r in db.conn.execute("SELECT * FROM pipeline")}
    touches = last_touches(db)
    my_name = db.get_setting("my_name") or ""
    rows = []
    for cid, s in scores.items():
        if s.total <= 0 or not s.fit_roles or (s.offlimits and s.offlimits.blocked):
            continue
        stage = stages.get(cid)
        if stage and stage["stage"] in SKIP_STAGES:
            continue
        recent = _recent_touch(db, cid, today)
        due = stage and stage["due_date"] and stage["due_date"] <= today.isoformat()
        if recent and not due:
            t = _local(recent)
            if t and _business_days_ago(t.date(), today) < QUIET_DAYS:
                continue
        co = companies[cid]
        people = db.conn.execute("SELECT * FROM people WHERE company_id = ?", (cid,)).fetchall()
        person, why_person = pick_person(people, s.fit_roles)
        if person and person["phone"] and not dialable(person["phone"]):
            person = dict(person) | {"phone": None}
        from . import leads as _leads
        lead = None if person else _leads.best(db, cid, s.fit_roles, ask_for(s.fit_roles))
        main = _main_line(db, cid, co["phone"])
        lt = touches.get(cid)
        # Candidates to pitch: best matches across this account's fitting roles, one per person.
        from .candidates import candidates_for_job
        pitches, seen = [], set()
        for title in (s.fit_roles[:4] if with_pitches else []):
            j = db.conn.execute("SELECT id FROM jobs WHERE company_id = ? AND title = ? AND closed_at IS NULL",
                                (cid, title)).fetchone()
            for m in candidates_for_job(db, j["id"], 3) if j else []:
                if m["c"]["id"] not in seen:
                    seen.add(m["c"]["id"])
                    pitches.append(m | {"job_id": j["id"], "job_title": title})
        pitches.sort(key=lambda m: (not m["local"], m["stale"]))
        pitches = pitches[:3]
        rows.append({
            "company_id": cid, "company": co["name"] or co["key"], "city": co["city"], "score": s,
                    "about": {"summary": co["summary"], "headcount": co["headcount"],
                              "headcount_source": co["headcount_source"], "city": co["city"]},
            "person": person, "why_person": why_person, "main_line": main,
            "why_now": [_tidy_reason(r, s.fit_roles) for pts, r in sorted(s.reasons, key=lambda x: -x[0])
                        if r and pts > 0][:3],
            "lead": lead, "lead_label": _leads.label(lead),
            "opener": opener(s, person["name"] if person else (lead["first_name"] if lead else None), co["city"], my_name,
                             pitches[0] if pitches else None, db.get_setting("my_company") or ""),
            "pitches": pitches, "ask_for": ask_for(s.fit_roles),
            "roles": open_roles(db, cid, s.fit_roles, today),
            "stage": STAGE_NAMES.get(stage["stage"]) if stage else None,
            "next_step": stage["next_step"] if stage else None, "due": bool(due),
            "last": lt,
        })
    # Due call-backs first, then accounts you can reach by phone, then by score.
    rows.sort(key=lambda r: (not r["due"], -r["score"].total,
                             not (r["person"] and r["person"]["phone"]) and not r["main_line"]))
    return rows[:limit]


def _local(ts: str) -> datetime | None:
    """Saved times are UTC; show and group them in this computer's time zone."""
    try:
        return datetime.fromisoformat(ts).astimezone()
    except (TypeError, ValueError):
        return None


def done_today(db: DB, today: date | None = None) -> list[dict]:
    day = today or date.today()
    since = (day - timedelta(days=2)).isoformat()
    out = []
    for r in db.conn.execute(
            "SELECT a.*, co.name AS company_name, p.name AS person_name FROM activities a "
            "LEFT JOIN companies co ON co.id = a.company_id LEFT JOIN people p ON p.id = a.person_id "
            f"WHERE a.kind IN ({','.join('?' * len(TOUCH_KINDS))}) AND a.created_at >= ? "
            "ORDER BY a.created_at DESC", (*TOUCH_KINDS, since)):
        t = _local(r["created_at"])
        if t and t.date() == day:
            out.append(dict(r) | {"time": t.strftime("%I:%M %p").lstrip("0")})
    return out


def log_outcome(db: DB, company_id: int, person_id: int | None, outcome: str, note: str = "") -> dict:
    """Record a call / email and move the account along. Returns what's needed to undo it."""
    from . import crm
    label = OUTCOMES.get(outcome)
    if not label:
        raise ValueError(outcome)
    before = crm.card(db, company_id)
    undo = {"company_id": company_id, "label": label,
            "before": dict(before) if before else None,
            "after_activity": db.conn.execute("SELECT COALESCE(MAX(id), 0) FROM activities").fetchone()[0],
            "active_enrollments": [r[0] for r in db.conn.execute(
                "SELECT id FROM enrollments WHERE company_id = ? AND status = 'active'", (company_id,))]}
    if outcome == "bad_number":
        # Not a touch: the account stays on the list with its next-best number (or email).
        undo["bad_number"] = retire_number(db, company_id, person_id, note)
        return undo
    kind = "email" if outcome == "emailed" else "call"
    body = label + (f": {note.strip()}" if note.strip() else "")
    crm.log(db, company_id, kind, body, person_id)
    db.commit()
    if outcome == "meeting":
        crm.set_stage(db, company_id, "meeting")
    elif outcome == "not_interested":
        crm.set_stage(db, company_id, "not_now", note.strip() or "not interested")
    else:
        crm.advance_to(db, company_id, "contacted")
        if person_id:
            crm.update_card(db, company_id, contact_id=person_id)
        if outcome in ("voicemail", "no_answer"):
            back = _add_business_days(date.today(), 2).isoformat()
            crm.update_card(db, company_id, next_step="Call back", due_date=back)
    db.commit()
    return undo


def undo_outcome(db: DB, undo: dict) -> None:
    """Put an account back exactly as it was before the last logged outcome."""
    cid = undo["company_id"]
    from .loxosync import forget_activities
    forget_activities(db, cid, undo["after_activity"])  # not sent to Loxo yet: never will be
    db.conn.execute("DELETE FROM activities WHERE company_id = ? AND id > ?", (cid, undo["after_activity"]))
    b = undo["before"]
    if b is None:
        db.conn.execute("DELETE FROM pipeline WHERE company_id = ?", (cid,))
    else:
        db.conn.execute("UPDATE pipeline SET stage = ?, contact_id = ?, next_step = ?, due_date = ?, lost_reason = ?, "
                        "updated_at = ? WHERE company_id = ?", (b["stage"], b["contact_id"], b["next_step"],
                                                                b["due_date"], b["lost_reason"], b["updated_at"], cid))
    bad = undo.get("bad_number")
    if bad:  # put the number back where it was
        db.conn.execute("DELETE FROM suppression WHERE value = ?", (bad["number"].lower(),))
        if bad["company_phone"]:
            db.update_company(cid, phone=bad["number"])
        for pid in bad["people"]:
            db.conn.execute("UPDATE people SET phone = ? WHERE id = ?", (bad["number"], pid))
        for c in bad["contacts"]:
            cols = list(c)  # same row id as before, so it sorts back into the same place
            db.conn.execute(f"INSERT OR IGNORE INTO contacts ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                            [c[k] for k in cols])
    for eid in undo["active_enrollments"]:  # follow-ups that a Meeting / Not interested stopped
        db.conn.execute("UPDATE enrollments SET status = 'active' WHERE id = ?", (eid,))
    db.commit()


def title_kind(title: str | None) -> str | None:
    """'Tax Manager, SALT' -> 'tax'; 'Partner-in-Charge of Audit' -> 'audit'; 'Partner' -> None."""
    r = (title or "").lower()
    core = re.split(r",| - | – | \(|\|", r)[0]  # "Software Engineer, Data & Cloud" is a software role
    return next((k for k, p, _, _ in _ASK_FOR if re.search(p, core)), None) \
        or next((k for k, p, _, _ in _ASK_FOR if re.search(p, r)), None)


def _role_votes(fit_roles: list[str]) -> tuple[dict[str, int], dict[str, int]]:
    """How many openings of each kind, and how many of those are director-level."""
    votes: dict[str, int] = {}
    senior: dict[str, int] = {}
    for role in fit_roles:
        kind = title_kind(role)
        if kind:
            votes[kind] = votes.get(kind, 0) + 1
            if _SENIOR.search(re.split(r",| - | – | \(|\|", role.lower())[0]):
                senior[kind] = senior.get(kind, 0) + 1
    return votes, senior


def main_kind(fit_roles: list[str]) -> str | None:
    votes, _ = _role_votes(fit_roles)
    return max(votes, key=lambda k: votes[k]) if votes else None


def ask_for(fit_roles: list[str]) -> str:
    """The title to ask for at the front desk / search on LinkedIn: the person who owns the hire."""
    # Go by the kind of role most of the openings are (ties: the first-listed role),
    # and for a director/head/VP opening ask for the person one level up.
    votes, senior = _role_votes(fit_roles)
    if not votes:
        return "hiring manager"
    best = max(votes, key=lambda k: votes[k])  # dict keeps first-seen order, so ties go to the first role
    if best in ("tax", "audit") and votes.get("tax") and votes.get("audit"):
        # Tax and audit/assurance openings together: a CPA firm, where partners own the hire.
        return "Tax Partner" if best == "tax" else "Audit Partner"
    for kind, _, normal, up in _ASK_FOR:
        if kind == best:
            # One level up only when most of these openings are themselves director-level.
            return up if senior.get(kind, 0) * 2 > votes[kind] else normal
    return "hiring manager"


# (kind, pattern, who owns the hire, who owns a director-level hire)
_ASK_FOR = [
    ("security", r"\b(security|cyber|soc|threat|incident)\b", "Director of Information Security", "CISO"),
    ("data", r"\b(data|machine learning|ml|ai|analytics|scientist)\b", "Director of Data Engineering",
     "VP of Data / Chief Data Officer"),
    ("cloud", r"\b(devops|cloud|site reliability|sre|platform|network)\b", "Director of Cloud / Platform Engineering",
     "VP of Engineering"),
    ("software", r"\b(software|developer|engineer in test|sdet)\b", "Director of Software Engineering",
     "VP of Engineering / CTO"),
    ("tax", r"\btax\b", "Tax Director", "VP of Tax / CFO"),
    ("audit", r"\b(audit|auditor|assurance)\b", "Audit Partner", "Managing Partner"),
    ("accounting", r"\b(accountant|accounting|controller)\b", "Controller", "CFO"),
    ("mep", r"\b(electrical|mechanical|mep|hvac|plumbing|fire protection|controls)\b", "MEP Engineering Manager",
     "Director of Engineering"),
    ("wealth", r"\b(wealth|financial planner|advisor)\b", "Managing Director, Wealth", "Managing Partner"),
    ("construction", r"\b(construction|field engineer|superintendent)\b", "Director of Construction",
     "VP of Construction"),
]
_SENIOR = re.compile(r"\b(director|head|vp|vice president|chief|partner|controller)\b")
