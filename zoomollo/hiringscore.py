"""Hiring score: which companies are most worth your time this week.

"My desk" (your niche) decides which open roles count:
  titles     - roles you place, e.g. "HVAC technician", "service manager". A job fits
               when every word of one entry appears in its title.
  exclude    - roles you don't work, e.g. "intern", "driver".
  locations  - cities / states you cover, e.g. "Austin, TX", "MO". Empty = anywhere.
  min_salary - yearly floor; only applied when a post states a salary.

Each company gets a 0-100 score with the reasons behind it (see SIGNALS).
Everything is computed from data already in Zoomollo, on your computer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from .db import DB

OPEN_DAYS = 30       # a role counts as open if it was still listed this recently
LONG_OPEN_DAYS = 30  # open this long = hard to fill

SIGNALS = """
+25 first open role in your niche, +10 each after (max 45)
+10 each niche role open 30+ days: hard to fill (max 20)
+10 no in-house recruiter found / -10 they have one
+10 reachable hiring manager for a niche role (found email or phone), +5 guessed email only
+2  each other open role: growing (max 10)
up to +10 permit / license activity in public records
+5  past client in Loxo: warm
off-limits companies score 0
"""


# --- my desk ---------------------------------------------------------------------

@dataclass
class Desk:
    titles: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    min_salary: int | None = None

    @property
    def is_set(self) -> bool:
        return bool(self.titles or self.exclude or self.locations or self.min_salary)


def _split(text: str | list | None, places: bool = False) -> list[str]:
    """Split a comma / semicolon / line separated list. For places, keep "Austin, TX" together."""
    if isinstance(text, list):
        items = text
    elif places:
        items = re.split(r"[\n;]+|,(?!\s*[A-Za-z]{2}\s*(?:,|;|$|\n))", text or "")
    else:
        items = re.split(r"[\n;,]+", text or "")
    return [i.strip() for i in items if i and i.strip()]


def load_desk(db: DB) -> Desk:
    raw = db.get_setting("desk")
    if not raw:
        return Desk()
    try:
        d = json.loads(raw)
    except ValueError:
        return Desk()
    return Desk(titles=_split(d.get("titles")), exclude=_split(d.get("exclude")),
                locations=_split(d.get("locations"), places=True), min_salary=d.get("min_salary") or None)


def save_desk(db: DB, titles="", exclude="", locations="", min_salary=None) -> Desk:
    try:
        floor = int(re.sub(r"[^\d]", "", str(min_salary))) if min_salary not in (None, "") else None
    except ValueError:
        floor = None
    if floor is not None and floor < 1000:  # "60" meaning 60k
        floor *= 1000
    desk = Desk(_split(titles), _split(exclude), _split(locations, places=True), floor or None)
    db.set_setting("desk", json.dumps(desk.__dict__))
    return desk


# --- matching roles -----------------------------------------------------------------

def _words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        out.add(w)
        if len(w) > 3 and w.endswith("s"):
            out.add(w[:-1])  # technicians -> technician
    return out


def _phrase_in(phrase: str, title_words: set[str]) -> bool:
    words = _words(phrase)
    return bool(words) and words <= title_words


def _location_ok(job_location: str | None, company_city: str | None, desk: Desk) -> bool:
    if not desk.locations:
        return True
    text = " ".join(x for x in [job_location, company_city] if x)
    if not text.strip():
        return True  # unknown location: don't rule it out
    from .discover import STATES, split_where
    for loc in desk.locations:
        if loc.lower() == "remote" and re.search(r"remote", text, re.I):
            return True
        city, state = split_where(loc)
        if len(loc.strip()) == 2 and loc.strip().upper() in STATES:
            city, state = "", loc.strip().upper()
        if city and re.search(rf"\b{re.escape(city)}\b", text, re.I):
            return True
        if not city and state and (re.search(rf"\b{state}\b", text) or
                                   re.search(rf"\b{STATES[state]}\b", text, re.I)):
            return True
    return False


def job_fit(title: str, location: str | None, company_city: str | None, salary_max: int | None,
            desk: Desk) -> tuple[bool, str]:
    """(fits, why not) for one role."""
    tw = _words(title)
    if any(_phrase_in(x, tw) for x in desk.exclude):
        return False, "excluded role"
    if desk.titles and not any(_phrase_in(x, tw) for x in desk.titles):
        return False, "not a role you place"
    if not _location_ok(location, company_city, desk):
        return False, "outside your area"
    if desk.min_salary and salary_max and salary_max < desk.min_salary:
        return False, "below your salary floor"
    return True, ""


def parse_salary(text: str) -> int | None:
    """Highest yearly salary stated in a job post, if any ($85,000 / $85k / $32/hr)."""
    best = None
    for m in re.finditer(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k\b|K\b)?\s*(/\s*(?:hr|hour)|per hour|an hour|hourly)?",
                         text or ""):
        num = float(m.group(1).replace(",", ""))
        if m.group(2):
            num *= 1000
        if m.group(3):
            num *= 2080
        if 15_000 <= num <= 1_000_000:
            best = max(best or 0, int(num))
    return best


def _days_since(value: str | None, today: date) -> int | None:
    if not value:
        return None
    v = str(value).strip()
    try:
        if v.isdigit():  # milliseconds since 1970 (Lever)
            d = datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc).date()
        else:
            d = date.fromisoformat(v[:10])
    except (ValueError, OverflowError, OSError):
        return None
    return (today - d).days


# --- scoring --------------------------------------------------------------------------

@dataclass
class Score:
    company_id: int
    total: int
    reasons: list[tuple[int, str]]
    fit_roles: list[str]
    other_roles: int
    offlimits: object = None
    stage: str | None = None

    @property
    def top_reasons(self) -> list[str]:
        return [r for _, r in sorted(self.reasons, key=lambda x: -abs(x[0])) if r][:3]


def score_all(db: DB, desk: Desk | None = None, today: date | None = None) -> dict[int, Score]:
    """Score every company in one pass (bulk queries; fine for thousands)."""
    from .offlimits import status as offlimits_status
    desk = desk or load_desk(db)
    today = today or date.today()
    companies = {r["id"]: r for r in db.conn.execute("SELECT * FROM companies")}
    jobs: dict[int, list] = {}
    for j in db.conn.execute("SELECT id, company_id, title, location, posted_at, first_seen, last_seen, salary_max "
                             "FROM jobs"):
        seen = _days_since(j["last_seen"] or j["first_seen"], today)
        if seen is None or seen <= OPEN_DAYS:
            jobs.setdefault(j["company_id"], []).append(j)
    people: dict[int, list] = {}
    for p in db.conn.execute("SELECT company_id, email, email_status, phone, hiring_for FROM people "
                             "WHERE hiring_for IS NOT NULL"):
        people.setdefault(p["company_id"], []).append(p)
    stages = {r["company_id"]: r["stage"] for r in db.conn.execute("SELECT company_id, stage FROM pipeline")}
    records = {}
    for cid, co in companies.items():
        n = sum(int(x) for x in re.findall(r"\((\d+) records\)", co["source"] or ""))
        if not n and co["source"] and re.search(r"permit|licen", co["source"], re.I):
            n = 1
        records[cid] = n

    out: dict[int, Score] = {}
    for cid, co in companies.items():
        reasons: list[tuple[int, str]] = []
        fit, other, long_open = [], 0, 0
        for j in jobs.get(cid, []):
            ok, _ = job_fit(j["title"], j["location"], co["city"], j["salary_max"], desk)
            if ok:
                fit.append(j["title"])
                age = _days_since(j["posted_at"], today)
                if age is None:
                    age = _days_since(j["first_seen"], today)
                if age is not None and age >= LONG_OPEN_DAYS:
                    long_open += 1
            else:
                other += 1
        if fit:
            pts = min(25 + 10 * (len(fit) - 1), 45)
            n = len(fit)
            label = f"role{'s' if n != 1 else ''} in your niche" if desk.titles else f"open role{'s' if n != 1 else ''}"
            reasons.append((pts, f"{n} {label}: " + ", ".join(dict.fromkeys(fit[:3]))))
        if long_open:
            reasons.append((min(10 * long_open, 20), f"{long_open} open {LONG_OPEN_DAYS}+ days (hard to fill)"))
        if other and desk.titles:
            reasons.append((min(2 * other, 10), f"{other} other open role{'s' if other != 1 else ''} (growing)"))
        # Only matters when they're hiring for roles you place.
        if fit and co["recruiters_seen"]:
            reasons.append((-10, "has an in-house recruiter"))
        elif fit and co["last_crawled"]:
            reasons.append((10, "no in-house recruiter found"))
        if fit:
            fit_set = set(fit)
            hms = [p for p in people.get(cid, []) if fit_set & {t.strip() for t in re.split(r";|\(\+", p["hiring_for"] or "")}]
            if any((p["email"] and p["email_status"] in ("found", "confirmed")) or p["phone"] for p in hms):
                reasons.append((10, "hiring manager reachable"))
            elif any(p["email"] for p in hms):
                reasons.append((5, "hiring manager email guessed"))
            else:
                reasons.append((0, "no hiring manager contact yet"))
        if records.get(cid):
            reasons.append((min(2 * records[cid], 10), f"active in public records ({records[cid]})"))
        st = offlimits_status(db, cid)
        if st.level == "warn" and "past client" in st.reason.lower():
            reasons.append((5, "past client: warm"))
        total = max(0, min(100, sum(p for p, _ in reasons)))
        if st.blocked:
            total = 0
            reasons.insert(0, (0, st.reason))
        out[cid] = Score(cid, total, reasons, fit, other, st, stages.get(cid))
    return out


def ranked(db: DB, desk: Desk | None = None, niche_only: bool = False, q: str = "",
           exclude_pipeline: bool = False, limit: int = 500) -> list[tuple[Score, object]]:
    """[(score, company row)] best first."""
    scores = score_all(db, desk)
    rows = {r["id"]: r for r in db.conn.execute("SELECT * FROM companies")}
    ql = q.lower()
    out = []
    for cid, s in scores.items():
        co = rows[cid]
        if niche_only and not s.fit_roles:
            continue
        if exclude_pipeline and (s.stage or s.offlimits.blocked):
            continue
        if ql and not any(ql in (co[k] or "").lower() for k in ("key", "name", "city", "industry")):
            continue
        out.append((s, co))
    out.sort(key=lambda x: (-x[0].total, -len(x[0].fit_roles), x[1]["name"] or x[1]["key"]))
    return out[:limit]
