"""Call blitz: build a list, then work it one flash card at a time.

A list comes from either your Today priorities or your own filters (vertical, area, who to
call, direct dials only, pipeline stage, minimum score, days since the last touch). Each
card has the number to dial, an opener, a voicemail script, a front-desk line and short
answers to the usual pushback. Outcomes are logged exactly like the Today page (same
pipeline moves, scoreboard counts and Loxo sync).
"""

from __future__ import annotations

import json
import re
from datetime import date
from urllib.parse import quote

from .db import DB
from .orgmap import SENIORITY_RANK, department
from . import today as td

VERTICALS = {
    "tax": ("CPA / tax / wealth", r"\b(cpa|tax|accounting|accountant|audit|assurance|wealth|financial|advisor)"),
    "mep": ("MEP engineering", r"\b(mep|mechanical|electrical|plumbing|hvac|engineering firm|permit|fire protection|"
                               r"controls|commissioning)"),
    "tech": ("AI, data, IT and cyber", r"\b(ai|artificial intelligence|machine learning|data|analytics|cyber|"
                                       r"security|it|software|cloud|devops|tech)"),
}
WHO = {
    "best": "Best person (hiring manager first)",
    "hm": "Likely hiring managers only",
    "leaders": "Directors, VPs and owners",
    "hr": "HR / talent",
    "main": "Main line only (front desk)",
}
STAGE_FILTERS = {
    "": "Any open account",
    "new": "Not in my pipeline yet",
    "prospect": "Prospect",
    "contacted": "Contacted",
    "due": "Call-backs due",
}
SORTS = {
    "priority": "Priority (call-backs, then score)",
    "score": "Highest score",
    "oldest": "Longest since last touch",
    "city": "By city",
}
DEFAULTS = {"source": "priority", "vertical": "", "city": "", "who": "best", "direct": "", "stage": "",
            "min_score": "0", "rest_days": "3", "fit_only": "", "size": "30", "sort": "priority"}

OUTCOME_KEYS = {"connected": "C", "voicemail": "V", "no_answer": "N", "meeting": "M", "not_interested": "X"}


def clean_filters(form) -> dict:
    f = {k: (form.get(k) if form.get(k) is not None else v) for k, v in DEFAULTS.items()}
    if "fit_only" not in form and form.get("source"):  # an unticked checkbox sends nothing
        f["fit_only"] = ""
    for k in ("min_score", "rest_days", "size"):
        try:
            f[k] = str(max(0, int(f[k] or 0)))
        except ValueError:
            f[k] = DEFAULTS[k]
    f["size"] = str(min(int(f["size"]) or 30, 200))
    for k, allowed in (("vertical", VERTICALS), ("who", WHO), ("stage", STAGE_FILTERS), ("sort", SORTS)):
        if f[k] not in allowed and not (k == "vertical" and f[k] == ""):
            f[k] = DEFAULTS[k]
    f["source"] = "custom" if f["source"] == "custom" else "priority"
    f["city"] = (f["city"] or "").strip()[:60]
    return f


def describe(f: dict) -> str:
    if f["source"] == "priority":
        return "Today's priorities"
    bits = [VERTICALS[f["vertical"]][0] if f["vertical"] else "All verticals"]
    if f["city"]:
        bits.append(f["city"])
    if f["who"] != "best":
        bits.append(WHO[f["who"]].lower())
    if f["direct"]:
        bits.append("direct dials")
    if f["stage"]:
        bits.append(STAGE_FILTERS[f["stage"]].lower())
    if int(f["min_score"]):
        bits.append(f"score {f['min_score']}+")
    return " · ".join(bits)


# ---- saved lists -------------------------------------------------------------------------

def saved_lists(db: DB) -> dict[str, dict]:
    try:
        return json.loads(db.get_setting("blitz_lists") or "{}")
    except ValueError:
        return {}


def save_list(db: DB, name: str, f: dict) -> None:
    lists = saved_lists(db)
    lists[name.strip()[:60]] = f
    db.set_setting("blitz_lists", json.dumps(lists))
    db.commit()


def delete_list(db: DB, name: str) -> None:
    lists = saved_lists(db)
    lists.pop(name, None)
    db.set_setting("blitz_lists", json.dumps(lists))
    db.commit()


# ---- building the list ------------------------------------------------------------------

def _vertical_of(co, fit_roles: list[str]) -> set[str]:
    text = " ".join([co["industry"] or "", co["source"] or "", *fit_roles]).lower()
    return {k for k, (_, rx) in VERTICALS.items() if re.search(rx, text)}


def _choose(people: list, fit_roles: list[str], who: str):
    """(person or None, why) for the 'who to call' filter. None + '' means skip the account."""
    level = lambda p: SENIORITY_RANK.get(p["seniority"] or "staff", 1)  # noqa: E731
    if who == "main":
        return None, "front desk"
    if who == "best":
        return td.pick_person(people, fit_roles)
    if who == "hm":
        hms = [p for p in people if p["hiring_for"]]
        return (max(hms, key=td._contact_score), "likely hiring manager") if hms else (None, "")
    if who == "hr":
        hr = [p for p in people if p["department"] == "hr"]
        return (max(hr, key=lambda p: (*td._contact_score(p), level(p))), "talent / HR") if hr else (None, "")
    # leaders: director and up, the role's own department first
    depts = {department(t) for t in fit_roles} - {"other"}
    lead = [p for p in people if p["seniority"] in ("director", "vp", "c_level")]
    if not lead:
        return None, ""
    best = max(lead, key=lambda p: (p["department"] in depts, *td._contact_score(p), level(p)))
    return best, "leads that team" if best["department"] in depts else "senior leader"


def build(db: DB, f: dict, today: date | None = None) -> list[dict]:
    """The blitz list: [{company_id, person_id, company, person, phone, kind}] in calling order."""
    today = today or date.today()
    size = int(f["size"]) or 30
    if f["source"] == "priority":
        rows = td.call_list(db, limit=size, with_pitches=False)
        out = []
        for r in rows:
            p = r["person"]
            phone, kind = _number(p, r["main_line"])
            if phone:
                out.append({"company_id": r["company_id"], "person_id": p["id"] if p else None,
                            "company": r["company"], "person": p["name"] if p else None, "phone": phone, "kind": kind})
        return out

    from .crm import STAGE_NAMES  # noqa: F401 - keeps import order the same as Today
    from .hiringscore import load_desk, score_all
    scores = score_all(db, load_desk(db))
    stages = {r["company_id"]: dict(r) for r in db.conn.execute("SELECT * FROM pipeline")}
    touches = td.last_touches(db)
    rest = int(f["rest_days"])
    min_score = int(f["min_score"])
    city = f["city"].lower()
    rows = []
    for co in db.conn.execute("SELECT * FROM companies"):
        cid = co["id"]
        s = scores.get(cid)
        if s is None or (s.offlimits and s.offlimits.blocked):
            continue
        if f["fit_only"] and not s.fit_roles:
            continue
        if s.total < min_score:
            continue
        stage = stages.get(cid)
        st = stage["stage"] if stage else None
        if st in td.SKIP_STAGES:
            continue
        due = bool(stage and stage["due_date"] and stage["due_date"] <= today.isoformat())
        if f["stage"] == "new" and stage:
            continue
        if f["stage"] in ("prospect", "contacted") and st != f["stage"]:
            continue
        if f["stage"] == "due" and not due:
            continue
        if f["vertical"] and f["vertical"] not in _vertical_of(co, s.fit_roles):
            continue
        if city and city not in (co["city"] or "").lower():
            continue
        recent = td._recent_touch(db, cid, today)
        if rest and recent and not due:
            t = td._local(recent)
            if t and td._business_days_ago(t.date(), today) < rest:
                continue
        people = db.conn.execute("SELECT * FROM people WHERE company_id = ?", (cid,)).fetchall()
        person, why = _choose(people, s.fit_roles, f["who"])
        if person is None and f["who"] not in ("best", "main"):
            continue
        if f["direct"] and not (person and person["phone"]):
            continue
        phone, kind = _number(person, td._main_line(db, cid, co["phone"]))
        if not phone:
            continue
        last = touches.get(cid)
        rows.append({"company_id": cid, "person_id": person["id"] if person else None,
                     "company": co["name"] or co["key"], "person": person["name"] if person else None,
                     "phone": phone, "kind": kind, "_score": s.total, "_due": due,
                     "_last": (last or {}).get("created_at") or "", "_city": co["city"] or "~"})
    if f["sort"] == "score":
        rows.sort(key=lambda r: -r["_score"])
    elif f["sort"] == "oldest":
        rows.sort(key=lambda r: (r["_last"], -r["_score"]))  # never touched ("") first
    elif f["sort"] == "city":
        rows.sort(key=lambda r: (r["_city"], -r["_score"]))
    else:
        rows.sort(key=lambda r: (not r["_due"], -r["_score"]))
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows[:size]]


def _number(person, main_line):
    if person and person["phone"]:
        return person["phone"], "direct"
    if main_line:
        return main_line, "main line"
    return None, None


# ---- one flash card -----------------------------------------------------------------------

def dial_digits(phone: str) -> str:
    """'+1 (512) 555-0100 x12' -> '+15125550100' (Google Voice wants the plain number)."""
    main = re.split(r"(?i)\s*(?:x|ext\.?|extension)\s*\d+\s*$", phone or "")[0]
    digits = re.sub(r"\D", "", main)
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits if digits else ""


def extension(phone: str) -> str | None:
    m = re.search(r"(?i)(?:x|ext\.?|extension)\s*(\d+)\s*$", phone or "")
    return m.group(1) if m else None


def gv_url(phone: str, account: str = "0") -> str:
    return f"https://voice.google.com/u/{account or '0'}/calls?a=nc,{quote(dial_digits(phone))}"


def _first(name: str | None) -> str:
    return (name or "").split()[0] if name else ""


def say_number(phone: str) -> str:
    """A number the way you read it out: 5125550123 -> 512-555-0123."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}" if len(digits) == 10 else (phone or "").strip()


def scripts(company: str, person_name: str | None, city: str | None, fit_roles: list[str], my_name: str,
            my_company: str, my_phone: str, ask_title: str) -> dict:
    """Voicemail, front-desk line and objection replies for one card."""
    role = td.clean_role(fit_roles[0]) if fit_roles else ""
    place = (city or "DFW").split(",")[0]
    me = " ".join(x for x in [my_name, f"with {my_company}" if my_company else ""] if x) or "a recruiter"
    first = _first(person_name) or "there"
    said = say_number(my_phone)
    num = f" My number is {said}." if said else ""
    again = f" Again, {said}." if said else ""
    topic = f"your {role} opening" if role else "your hiring plans this year"
    voicemail = (f"Hi {first}, this is {me}.{num} I recruit {role + 's' if role and not role.endswith('s') else role or 'talent'} "
                 f"in {place} and had a quick question about {topic}. I'll send a short email too.{again}")
    front = (f"Hi, this is {_first(my_name) or 'calling'}{' with ' + my_company if my_company else ''}. "
             f"Could you point me to whoever runs hiring for {('the ' + role + ' role') if role else 'your team'}? "
             f"Usually that's the {ask_title}.")
    objections = [
        ("We don't use recruiters / agencies",
         "Totally fair, most of my clients said that first. I only call when I have someone who fits. "
         "If I send one profile, would you look at it?"),
        ("Just send me an email",
         f"Happy to. So it's worth opening: is {topic.replace('your ', 'the ')} still the priority, "
         "or is something else more urgent?"),
        ("The role is filled / not hiring",
         "Good to hear. When you do add someone, what's usually the hardest seat to fill? "
         "I'll keep an eye out and check back next quarter."),
        ("We already have vendors",
         "Makes sense. Most teams keep one or two. When they come up short on a search, "
         "I'm happy to be the backup. Can I send you one strong profile?"),
        ("Bad time",
         "No problem. Is later today or tomorrow morning better? I'll keep it to two minutes."),
    ]
    return {"voicemail": voicemail, "front_desk": front, "objections": objections}


def front_desk_for(lead: dict | None, base: str, my_name: str, my_company: str) -> str:
    if not lead:
        return base
    who = lead["first_name"] + (f", your {lead['title']}" if lead.get("title") else "")
    return (f"Hi, this is {_first(my_name) or 'calling'}{' with ' + my_company if my_company else ''}. "
            f"Could you put me through to {who}?")


def card(db: DB, item: dict, settings: dict) -> dict:
    """Everything shown on one flash card."""
    from .candidates import candidates_for_job
    from .crm import STAGE_NAMES
    from .hiringscore import load_desk, score_all
    cid = item["company_id"]
    co = db.company_row(cid)
    s = score_all(db, load_desk(db)).get(cid)
    fit = s.fit_roles if s else []
    person = db.conn.execute("SELECT * FROM people WHERE id = ?", (item["person_id"],)).fetchone() \
        if item.get("person_id") else None
    main = td._main_line(db, cid, co["phone"] if co else None)
    phone, kind = _number(person, main)
    pitches, seen = [], set()
    for title in fit[:3]:
        j = db.conn.execute("SELECT id FROM jobs WHERE company_id = ? AND title = ? AND closed_at IS NULL",
                            (cid, title)).fetchone()
        for m in candidates_for_job(db, j["id"], 2) if j else []:
            if m["c"]["id"] not in seen:
                seen.add(m["c"]["id"])
                pitches.append(m | {"job_id": j["id"], "job_title": title})
    pitches.sort(key=lambda m: (not m["local"], m["stale"]))
    pitches = pitches[:2]
    city = co["city"] if co else None
    ask = td.ask_for(fit)
    from . import leads as _leads
    lead = None if person else _leads.best(db, cid, fit, ask)
    if s and fit:
        say = td.opener(s, person["name"] if person else (lead["first_name"] if lead else None), city, settings["my_name"],
                        pitches[0] if pitches else None, settings["my_company"])
    else:
        me = " ".join(x for x in [_first(settings["my_name"]), f"with {settings['my_company']}"
                                  if settings["my_company"] else ""] if x)
        place = (city or "DFW").split(",")[0]
        if person:
            say = (f"Hi {_first(person['name'])}{', ' + me if me else ''}. I'll be quick: I recruit for firms like "
                   f"yours in {place}. Are you adding anyone to the team this year?")
        else:
            say = (f"Hi, this is {me or 'calling'}. Who handles hiring for your team? "
                   f"I have a quick question for them.")
    stage = db.conn.execute("SELECT * FROM pipeline WHERE company_id = ?", (cid,)).fetchone()
    last = td.last_touches(db).get(cid)
    why = [td._tidy_reason(r, fit) for pts, r in sorted(s.reasons, key=lambda x: -x[0]) if r and pts > 0][:3] if s else []
    return {
        "company_id": cid, "company": (co["name"] or co["key"]) if co else item.get("company"),
            "about": ({"summary": co["summary"], "headcount": co["headcount"],
                       "headcount_source": co["headcount_source"], "city": co["city"]} if co else {}),
        "city": city, "domain": co["domain"] if co else None, "score": s.total if s else 0,
        "person": person, "why_person": "" if not person else _why(person, fit),
        "phone": phone, "kind": kind, "ext": extension(phone or ""), "main_line": main,
        "gv": gv_url(phone, settings["gv_account"]) if phone else None,
        "opener": say, "why_now": why, "ask_for": ask, "pitches": pitches,
        "lead": lead, "lead_label": _leads.label(lead),
        "stage": STAGE_NAMES.get(stage["stage"]) if stage else None,
        "next_step": stage["next_step"] if stage else None, "last": last,
        **_with_lead(scripts(co["name"] if co else "", person["name"] if person else (lead["first_name"] if lead else None),
                             city, fit, settings["my_name"], settings["my_company"], settings["my_phone"], ask),
                     lead, settings),
    }


def _with_lead(sc: dict, lead, settings) -> dict:
    sc["front_desk"] = front_desk_for(lead, sc["front_desk"], settings["my_name"], settings["my_company"])
    return sc


def _why(person, fit_roles: list[str]) -> str:
    _, why = td.pick_person([person], fit_roles)
    return why or (person["title"] or "")


def settings_of(db: DB) -> dict:
    import os
    return {"my_name": db.get_setting("my_name") or os.environ.get("MY_NAME") or "",
            "my_company": db.get_setting("my_company") or os.environ.get("MY_COMPANY") or "",
            "my_phone": db.get_setting("my_phone") or "",
            "dialer": db.get_setting("blitz_dialer") or "gv",
            "gv_account": db.get_setting("gv_account") or "0"}


# ---- the session --------------------------------------------------------------------------

def load_session(db: DB) -> dict | None:
    try:
        s = json.loads(db.get_setting("blitz_session") or "null")
    except ValueError:
        return None
    return s if s and s.get("items") is not None else None


def save_session(db: DB, s: dict | None) -> None:
    db.set_setting("blitz_session", json.dumps(s) if s else "")
    db.commit()


def tally(s: dict) -> dict:
    t = {k: 0 for k in ("dials", "connected", "voicemail", "no_answer", "meeting", "not_interested", "skipped")}
    for e in s.get("log", []):
        if e["outcome"] == "skip":
            t["skipped"] += 1
        elif e["outcome"] == "bad_number":
            t["bad_number"] = t.get("bad_number", 0) + 1   # not a dial that counts
        else:
            t["dials"] += 1
            t[e["outcome"]] = t.get(e["outcome"], 0) + 1
    t["conversations"] = t["connected"] + t["meeting"] + t["not_interested"]
    t["connect_rate"] = round(100 * t["conversations"] / t["dials"]) if t["dials"] else 0
    return t
