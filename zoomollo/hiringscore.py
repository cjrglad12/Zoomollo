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
from functools import lru_cache

from .db import DB

OPEN_DAYS = 30       # a role counts as open if it was still listed this recently
LONG_OPEN_DAYS = 30  # open this long = hard to fill
STALE_DAYS = 365     # posted this long ago = probably a standing ad

SIGNALS = """
+25 first open role in your niche, +10 each after (max 45)
+10 each niche role open 30+ days: hard to fill (max 20)
+10 no in-house recruiter found / -10 they have one
+10 reachable hiring manager for a niche role (found email or phone), +5 guessed email only
+2  each other open role: growing (max 10)
up to +10 permit / license activity in public records
+5  past client in Loxo: warm
+10 a niche role was reposted (hard to fill)
+5  a niche role was posted this week
+5  a new leader joined (last 30 days): new managers often hire
off-limits companies score 0
"""


# --- my desk ---------------------------------------------------------------------

@dataclass
class Desk:
    titles: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    min_salary: int | None = None
    max_open_roles: int | None = None  # skip companies bigger than this (open roles is a rough size gauge)
    min_headcount: int | None = None   # target company size, in total employees ("100-500ish")
    max_headcount: int | None = None

    @property
    def is_set(self) -> bool:
        return bool(self.titles or self.exclude or self.locations or self.min_salary)


# National giants you don't work with even when only a few of their openings are loaded.
LARGE_FIRMS = {
    # Big 4 and the largest national accounting / advisory firms
    "deloitte.com", "pwc.com", "ey.com", "kpmg.com", "kpmg.us", "bdo.com", "rsmus.com", "grantthornton.com",
    "claconnect.com", "forvismazars.com", "forvis.com", "crowe.com", "bakertilly.com", "cbiz.com",
    "eisneramper.com", "marcumllp.com", "cohnreznick.com", "plantemoran.com", "mossadams.com", "wipfli.com",
    "cherrybekaert.com", "aprio.com", "ryan.com", "thinkbrg.com", "accenture.com", "protiviti.com",
    "fticonsulting.com", "alvarezandmarsal.com", "huronconsultinggroup.com", "avantax.com",
    "avantaxwealthmanagement.com",
    # Big banks, insurers and tech
    "bankofamerica.com", "business.bofa.com", "jpmorganchase.com", "chase.com", "wellsfargo.com", "citi.com",
    "53.com", "frostbank.com", "principal.com", "statefarm.com", "ibm.com", "microsoft.com", "oracle.com",
    "sabre.com", "solera.com", "omnitracs.com", "trendmicro.com", "tylertech.com", "childrens.com",
    # National builders and the biggest Texas contractors
    "tcco.com", "turnerconstruction.com", "beckgroup.com", "austin-ind.com", "austincommercial.com",
    "balfourbeattyus.com", "dpr.com", "skanska.com", "usa.skanska.com", "whiting-turner.com", "jedunn.com",
    "mccarthy.com", "henselphelps.com", "manhattanconstructiongroup.com", "kiewit.com", "mortenson.com",
    "gilbaneco.com", "structuretone.com", "clarkconstruction.com", "swinerton.com", "walkertx.com",
    "spawglass.com", "zachrygroup.com", "fluor.com", "jacobs.com", "aecom.com", "kimley-horn.com",
}


def too_large(domain: str | None, open_roles: int, desk: Desk) -> str | None:
    """Why this company is bigger than you work with, or None."""
    d = (domain or "").lower().removeprefix("www.")
    if any(d == x or d.endswith("." + x) for x in LARGE_FIRMS):
        return "large national firm"
    if desk.max_open_roles and open_roles > desk.max_open_roles:
        return f"too large: {open_roles} open roles (your limit is {desk.max_open_roles})"
    return None


SIZE_SLACK = 0.25  # "100-500ish": a company up to a quarter outside the range still counts


def wrong_size(headcount: int | None, desk: Desk) -> str | None:
    """Why this company's headcount is outside your target range, or None (also None when unknown)."""
    if not headcount or not (desk.min_headcount or desk.max_headcount):
        return None
    lo, hi = desk.min_headcount, desk.max_headcount
    want = f"{lo or 1:,}–{hi:,}" if hi else f"{lo:,}+"
    if lo and headcount < lo * (1 - SIZE_SLACK):
        return f"too small: about {headcount:,} people (you target {want})"
    if hi and headcount > hi * (1 + SIZE_SLACK):
        return f"too large: about {headcount:,} people (you target {want})"
    return None


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
                locations=_split(d.get("locations"), places=True), min_salary=d.get("min_salary") or None,
                max_open_roles=d.get("max_open_roles") or None,
                min_headcount=d.get("min_headcount") or None, max_headcount=d.get("max_headcount") or None)


def _count(value, current):
    """A number typed into a form ("500", "1,000", blank to clear); "keep" leaves it as it was."""
    if value == "keep":
        return current
    digits = re.sub(r"[^\d]", "", str(value or ""))
    return int(digits) if digits and int(digits) > 0 else None


def save_desk(db: DB, titles="", exclude="", locations="", min_salary=None, max_open_roles="keep",
              min_headcount="keep", max_headcount="keep") -> Desk:
    try:
        floor = int(re.sub(r"[^\d]", "", str(min_salary))) if min_salary not in (None, "") else None
    except ValueError:
        floor = None
    if floor is not None and floor < 1000:  # "60" meaning 60k
        floor *= 1000
    if max_open_roles == "keep":  # callers that don't show this setting leave it as it was
        cap = load_desk(db).max_open_roles
    else:
        digits = re.sub(r"[^\d]", "", str(max_open_roles or ""))
        cap = int(digits) if digits else None
    old = load_desk(db)
    lo, hi = _count(min_headcount, old.min_headcount), _count(max_headcount, old.max_headcount)
    if lo and hi and lo > hi:
        lo, hi = hi, lo
    desk = Desk(_split(titles), _split(exclude), _split(locations, places=True), floor or None, cap or None, lo, hi)
    db.set_setting("desk", json.dumps(desk.__dict__))
    return desk


# --- matching roles -----------------------------------------------------------------

def _words(text: str) -> frozenset[str]:
    return _words_of(text or "")


@lru_cache(maxsize=50_000)
def _words_of(text: str) -> frozenset[str]:
    out = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        out.add(w)
        if len(w) > 3 and w.endswith("s"):
            out.add(w[:-1])  # technicians -> technician
    return frozenset(out)


def _phrase_in(phrase: str, title_words: set[str]) -> bool:
    words = _words(phrase)
    return bool(words) and words <= title_words


def _location_ok(job_location: str | None, company_city: str | None, desk: Desk, title: str | None = None) -> bool:
    if not desk.locations:
        return True
    # The same role is asked about on every page: remember the answer (it only depends on these four things).
    return _location_ok_for(job_location, company_city, tuple(desk.locations), title)


class _Area:
    def __init__(self, locations):
        self.locations = list(locations)


@lru_cache(maxsize=60_000)
def _location_ok_for(job_location, company_city, locations: tuple, title) -> bool:
    desk = _Area(locations)
    # The job's own location decides; the company's home city only fills in when the post
    # gives none (a Bengaluru role at a Dallas company is not a Dallas role).
    text = (job_location or "").strip() or (company_city or "")
    if not (job_location or "").strip() and title and _title_says_elsewhere(title, desk):
        return False  # no location field, but the title names somewhere else ("… Bengaluru, Karnataka")
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


_ELSEWHERE = re.compile(
    r"\b(india|bengaluru|bangalore|hyderabad|pune|chennai|mumbai|karnataka|noida|gurgaon|philippines|manila|"
    r"mexico|canada|toronto|vancouver|uk|united kingdom|london|ireland|dublin|germany|europe|emea|apac|latam|"
    r"chile|brazil|argentina|colombia|costa rica|poland|romania|ukraine|singapore|australia|japan|china|"
    r"israel|netherlands|spain|france)\b", re.I)


def _title_says_elsewhere(title: str, desk: Desk) -> bool:
    """A post with no location field that names another country or US state in its title."""
    from .discover import STATES
    if _ELSEWHERE.search(title or ""):
        return True
    # "Senior Data Engineer - Austin, TX": a city named in the title that isn't one of yours.
    my_cities = {loc.split(",")[0].strip().lower() for loc in desk.locations}
    states = "|".join(list(STATES) + [re.escape(n) for n in STATES.values()])
    for city in re.findall(rf"\b([A-Z][a-z]+(?: [A-Z][a-z]+)?),\s*(?:{states})\b", title or ""):
        names = {city.lower(), city.split()[-1].lower()}  # "Aezion Frisco, Texas" -> also "frisco"
        if not names & my_cities and not names & {"remote", "hybrid", "onsite", "on-site"}:
            return True
    covered = {s for loc in desk.locations for s in re.findall(r"\b([A-Z]{2})\b", loc)}
    for abbr, name in STATES.items():
        if abbr not in covered and re.search(rf"\b{re.escape(name)}\b", title or "", re.I) and name.lower() != "washington":
            return True
    return False


def job_fit(title: str, location: str | None, company_city: str | None, salary_max: int | None,
            desk: Desk) -> tuple[bool, str]:
    """(fits, why not) for one role."""
    key = getattr(desk, "_memo_key", None)
    if key is None:
        key = repr(desk)
        try:
            object.__setattr__(desk, "_memo_key", key)
        except Exception:  # noqa: BLE001
            pass
    return _job_fit(title, location, company_city, salary_max, key, desk)


_FIT: dict = {}


def _job_fit(title, location, company_city, salary_max, key, desk) -> tuple[bool, str]:
    k = (title, location, company_city, salary_max, key)
    if k not in _FIT:
        if len(_FIT) > 80_000:
            _FIT.clear()
        _FIT[k] = _job_fit_now(title, location, company_city, salary_max, desk)
    return _FIT[k]


def _job_fit_now(title, location, company_city, salary_max, desk) -> tuple[bool, str]:
    tw = _words(title)
    if any(_phrase_in(x, tw) for x in desk.exclude):
        return False, "excluded role"
    if desk.titles and not any(_phrase_in(x, tw) for x in desk.titles):
        return False, "not a role you place"
    if not _location_ok(location, company_city, desk, title):
        return False, "outside your area"
    if desk.min_salary and salary_max and salary_max < desk.min_salary:
        return False, "below your salary floor"
    if desk.min_salary and desk.min_salary >= 70000 and not salary_max and _ENTRY_LEVEL.search(title or ""):
        return False, "entry-level: likely below your salary floor"
    if _RESIDENTIAL.search(title or ""):
        return False, "residential"
    return True, ""


# Most posts don't state pay. These titles almost never pay $70k+ base in DFW.
_ENTRY_LEVEL = re.compile(
    r"(?i)\b(intern(ship)?|junior|jr\.?|entry[- ]level|new grad\w*|campus|apprentice|trainee|"
    r"(fall|spring|summer|winter) 20\d\d|staff accountant|trust accountant|bookkeeper|clerk)\b"
    r"|\b(engineer|developer|analyst|accountant|advisor)\s+(1|i)\b"
    r"|(?<!senior )(?<!sr\. )(?<!sr )\bassociate\b(?! or senior)(?! director)")
_RESIDENTIAL = re.compile(r"(?i)\b(residential|homebuild\w*|home ?builder|single[- ]family)\b")
_RESIDENTIAL_CO = re.compile(r"(?i)\b(residential|homebuild\w*|home ?builders?|homes|home services|heating (&|and) "
                             r"air|remodel\w*|roofing|pools?|plumbing (&|and) drain|income tax|tax prep\w*)\b")


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


_SCORES: dict = {}


def score_all(db: DB, desk: Desk | None = None, today: date | None = None) -> dict[int, Score]:
    """Every company's score. Worked out once, then reused until something is saved to the database."""
    desk = desk or load_desk(db)
    today = today or date.today()
    stamp = None if db.conn.in_transaction else db.stamp()
    if not stamp or stamp[0] is None:
        return _score_all(db, desk, today)
    key = (db.path, stamp, repr(desk), today)
    if key not in _SCORES:
        _SCORES.clear()
        _SCORES[key] = _score_all(db, desk, today)
    return _SCORES[key]


def _score_all(db: DB, desk: Desk, today: date) -> dict[int, Score]:
    """Score every company in one pass (bulk queries; fine for thousands)."""
    from .offlimits import status as offlimits_status
    companies = {r["id"]: r for r in db.conn.execute("SELECT * FROM companies")}
    jobs: dict[int, list] = {}
    for j in db.conn.execute("SELECT id, company_id, title, location, posted_at, first_seen, last_seen, salary_max "
                             "FROM jobs WHERE closed_at IS NULL"):
        seen = _days_since(j["last_seen"] or j["first_seen"], today)
        if seen is None or seen <= OPEN_DAYS:
            jobs.setdefault(j["company_id"], []).append(j)
    people: dict[int, list] = {}
    for p in db.conn.execute("SELECT company_id, email, email_status, phone, hiring_for FROM people "
                             "WHERE hiring_for IS NOT NULL"):
        people.setdefault(p["company_id"], []).append(p)
    stages = {r["company_id"]: r["stage"] for r in db.conn.execute("SELECT company_id, stage FROM pipeline")}
    from .recheck import recent_signals
    changes = recent_signals(db, today)
    records = {}
    for cid, co in companies.items():
        n = sum(int(x) for x in re.findall(r"\((\d+) records\)", co["source"] or ""))
        if not n and co["source"] and re.search(r"permit|licen", co["source"], re.I):
            n = 1
        records[cid] = n

    out: dict[int, Score] = {}
    for cid, co in companies.items():
        reasons: list[tuple[int, str]] = []
        fit, other, long_open, unplaced, stale = [], 0, 0, 0, 0
        for j in jobs.get(cid, []):
            ok, _ = job_fit(j["title"], j["location"], co["city"], j["salary_max"], desk)
            if ok:
                fit.append(j["title"])
                if desk.locations and not (j["location"] or "").strip():
                    unplaced += 1  # counted as local only because of the company's address
                age = _days_since(j["posted_at"], today)
                if age is None:
                    age = _days_since(j["first_seen"], today)
                if age is not None and age >= STALE_DAYS:
                    stale += 1  # left up for over a year: often a standing ad, not a live opening
                elif age is not None and age >= LONG_OPEN_DAYS:
                    long_open += 1
            else:
                other += 1
        if fit:
            n = len(fit)
            placed = n - unplaced
            # Roles whose post states a local location count fully; roles with no location
            # (usually read off a careers page) count for less until confirmed.
            pts = min((25 + 10 * (placed - 1) if placed else 0) + 8 * unplaced, 45)
            if not placed:
                pts = min(pts, 16)
            label = f"role{'s' if n != 1 else ''} in your niche" if desk.titles else f"open role{'s' if n != 1 else ''}"
            note = (f" ({unplaced} with no location stated)" if placed and unplaced
                    else " (location not stated)" if unplaced else "")
            reasons.append((pts, f"{n} {label}: " + ", ".join(dict.fromkeys(fit[:3])) + note))
        if long_open:
            reasons.append((min(10 * long_open, 20), f"{long_open} open {LONG_OPEN_DAYS}+ days (hard to fill)"))
        if stale:
            reasons.append((-15 if stale == len(fit) else -5,
                            f"{stale} posted over a year ago (ask if it is still open)"))
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
        ch = changes.get(cid)
        if ch and fit:
            fit_set = set(fit)
            reposted = [t for t in ch["reposted"] if t in fit_set]
            new = [t for t in ch["new"] if t in fit_set and t not in reposted]
            if reposted:
                reasons.append((10, f"reposted: {', '.join(dict.fromkeys(reposted))} (hard to fill)"))
            if new:
                reasons.append((5, "new this week: " + ", ".join(dict.fromkeys(new))))
        if ch and ch["leaders"]:
            reasons.append((5, "new leader on their team (new managers often hire)"))
        if records.get(cid):
            reasons.append((min(2 * records[cid], 10), f"active in public records ({records[cid]})"))
        st = offlimits_status(db, cid)
        if st.level == "warn" and "past client" in st.reason.lower():
            reasons.append((5, "past client: warm"))
        total = max(0, min(100, sum(p for p, _ in reasons)))
        big = too_large(co["domain"] or co["key"], len(jobs.get(cid, [])), desk) if fit else None
        if fit and not big and _RESIDENTIAL_CO.search(" ".join(x or "" for x in (co["name"], co["industry"]))):
            big = "residential / home services company"
        if fit and not big:
            from .geo import abroad_reason
            big = abroad_reason([j["location"] for j in jobs.get(cid, [])], co["abroad_note"])
        if fit and not big:
            big = wrong_size(co["headcount"], desk)
            if not big and (desk.min_headcount or desk.max_headcount):
                reasons.append((0, f"about {co['headcount']:,} people" if co["headcount"] else "headcount not known yet"))
        if st.blocked:
            total = 0
            reasons.insert(0, (0, st.reason))
        elif big:
            total = 0
            reasons.insert(0, (0, big))
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
