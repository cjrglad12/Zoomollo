"""Candidates: from Loxo or uploaded résumés, searched through the keyword lens,
and bridged to companies hiring for what they do.

Loxo: the candidate list (title, company, location, skills, tags, contact
details) syncs in a couple of minutes. Résumés are downloaded gradually in the
background (two requests each), and candidates matching your current search
jump the queue. Everything is stored on this computer.
"""

from __future__ import annotations

from datetime import date

import re
from collections import deque

from . import lens
from .db import DB, now
from .resumes import basics, extract_text, years_experience

# Résumés to fetch next (candidate ids); search results are pushed to the front.
RESUME_QUEUE: deque[int] = deque()


# --- storing ----------------------------------------------------------------------

def _names(items) -> str:
    out = []
    for x in items or []:
        if isinstance(x, dict):
            out.append(str(x.get("name") or x.get("value") or ""))
        elif x:
            out.append(str(x))
    return ", ".join(o for o in out if o)


def _first_value(items) -> str | None:
    for x in items or []:
        v = x.get("value") if isinstance(x, dict) else x
        if v:
            return str(v)
    return None


def upsert_loxo(db: DB, p: dict) -> int | None:
    types = {(t.get("name") if isinstance(t, dict) else str(t)).lower() for t in p.get("person_types") or []}
    if types and "candidate" not in types:
        return None  # a client contact, not a candidate
    skills = "; ".join(x for x in [_names(p.get("skillsets")), _names(p.get("all_raw_tags")),
                                    re.sub(r"<[^>]+>", " ", p.get("description") or "")] if x.strip())
    row = db.conn.execute("SELECT id, updated_at, resume_status FROM candidates WHERE loxo_id = ?", (p["id"],)).fetchone()
    vals = dict(name=p.get("name") or " ".join(x for x in [p.get("first_name"), p.get("last_name")] if x),
                title=p.get("current_title"), company=p.get("current_company"), city=p.get("city"),
                state=p.get("state"), email=_first_value(p.get("emails")), phone=_first_value(p.get("phones")),
                linkedin_url=p.get("linkedin_url"), skills=skills[:5000] or None, updated_at=p.get("updated_at"))
    if row:
        changed = (row["updated_at"] or "") != (vals["updated_at"] or "")
        cols = ", ".join(f"{k} = ?" for k in vals)
        db.conn.execute(f"UPDATE candidates SET {cols}" + (", resume_status = NULL" if changed and row["resume_status"] != "queued" else "")
                        + " WHERE id = ?", (*vals.values(), row["id"]))
        return row["id"]
    cols = ", ".join(vals)
    return db.conn.execute(f"INSERT INTO candidates (source, loxo_id, {cols}, created_at) VALUES ('loxo', ?, "
                           + ", ".join("?" * len(vals)) + ", ?)", (p["id"], *vals.values(), now())).lastrowid


def sync_loxo(db: DB, client=None, progress=None) -> dict:
    """Candidate list from Loxo (no résumés; those come gradually)."""
    from .loxo import LoxoClient
    say = progress or (lambda m: None)
    client = client or LoxoClient()
    seen = kept = 0
    params: dict = {"per_page": 100}
    last = None
    while True:
        data = client.get("people", **params)
        people = data.get("people") or []
        if not people:
            break
        for p in people:
            seen += 1
            if upsert_loxo(db, p):
                kept += 1
        db.commit()
        if seen % 2000 < 100:
            say(f"Loxo people read: {seen}")
        sid = data.get("scroll_id")
        if not sid or sid == last:
            break
        last = params["scroll_id"] = sid
    db.set_setting("candidates_synced_at", now())
    return {"people": seen, "candidates": kept}


def fetch_resume(db: DB, candidate_id: int, client=None) -> str:
    """Download and read one Loxo candidate's newest résumé. Returns the status."""
    import requests

    from .loxo import LoxoClient
    c = db.conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    if not c or not c["loxo_id"]:
        return "none"
    client = client or LoxoClient()
    detail = client.get(f"people/{c['loxo_id']}")
    profile = []
    for jp in detail.get("job_profiles") or []:
        comp = jp.get("company")
        comp_name = jp.get("company_name") or (comp.get("name") if isinstance(comp, dict) else comp)
        profile.append(" ".join(str(x) for x in [jp.get("title"), comp_name, jp.get("start_date") or jp.get("started_at"),
                                                 jp.get("end_date")] if x))
    for ep in detail.get("education_profiles") or []:
        profile.append(" ".join(str(x) for x in [ep.get("degree"), ep.get("school") or ep.get("school_name"),
                                                 ep.get("end_date") or ep.get("graduation_year")] if x))
    resumes = sorted(detail.get("resumes") or [], key=lambda r: r.get("updated_at") or r.get("created_at") or "",
                     reverse=True)
    status, text, name = "none", None, None
    for r in resumes[:2]:
        url = r.get("download_url")
        if not url:
            continue
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            text = extract_text(resp.content, r.get("name") or "", resp.headers.get("Content-Type", ""))
            name, status = r.get("name"), "fetched"
            break
        except (requests.RequestException, ValueError, Exception) as e:  # noqa: BLE001
            status = f"failed: {str(e)[:120]}"
    db.conn.execute("UPDATE candidates SET resume_text = COALESCE(?, resume_text), resume_name = COALESCE(?, resume_name), "
                    "resume_status = ?, resume_fetched_at = ?, profile = ? WHERE id = ?",
                    (text, name, status, now(), "\n".join(x for x in profile if x) or None, candidate_id))
    db.commit()
    return status


def resume_backlog(db: DB) -> int:
    return db.conn.execute("SELECT COUNT(*) FROM candidates WHERE source = 'loxo' AND resume_status IS NULL").fetchone()[0]


def fill_resumes(db: DB, limit: int = 100, client=None, progress=None) -> int:
    """Fetch résumés: queued (search results) first, then most recently updated."""
    done = 0
    while done < limit:
        cid = RESUME_QUEUE.popleft() if RESUME_QUEUE else None
        if cid is None:
            row = db.conn.execute("SELECT id FROM candidates WHERE source = 'loxo' AND resume_status IS NULL "
                                  "ORDER BY updated_at DESC LIMIT 1").fetchone()
            if not row:
                break
            cid = row["id"]
        else:
            st = db.conn.execute("SELECT resume_status FROM candidates WHERE id = ?", (cid,)).fetchone()
            if not st or st[0] not in (None, "queued"):
                continue
        try:
            fetch_resume(db, cid, client)
        except Exception as e:  # noqa: BLE001 - mark and move on
            db.conn.execute("UPDATE candidates SET resume_status = ? WHERE id = ?", (f"failed: {str(e)[:120]}", cid))
            db.commit()
        done += 1
    if done and progress:
        progress(f"✓ Résumés downloaded: {done} ({resume_backlog(db)} to go)")
    return done


def add_upload(db: DB, filename: str, data: bytes) -> int:
    """An uploaded résumé file becomes a candidate (matched by email to avoid duplicates)."""
    text = extract_text(data, filename)
    b = basics(text)
    name = b.get("name") or re.sub(r"[_-]+", " ", filename.rsplit(".", 1)[0]).strip().title()
    existing = db.conn.execute("SELECT id FROM candidates WHERE email = ? AND email IS NOT NULL",
                               (b.get("email"),)).fetchone() if b.get("email") else None
    if existing:
        db.conn.execute("UPDATE candidates SET resume_text = ?, resume_name = ?, resume_status = 'fetched', "
                        "resume_fetched_at = ? WHERE id = ?", (text, filename, now(), existing["id"]))
        db.commit()
        return existing["id"]
    cid = db.conn.execute(
        "INSERT INTO candidates (source, name, title, city, state, email, phone, resume_text, resume_name, resume_status, "
        "resume_fetched_at, created_at) VALUES ('upload', ?, ?, ?, ?, ?, ?, ?, ?, 'fetched', ?, ?)",
        (name, _guess_title(text, name), b.get("city"), b.get("state"), b.get("email"), b.get("phone"), text, filename,
         now(), now())).lastrowid
    db.commit()
    return cid


def _guess_title(text: str, name: str) -> str | None:
    from .extract import _TITLE_HINT
    for line in text.splitlines()[:15]:
        line = re.sub(r"\(.*?\)|\b(?:19|20)\d{2}\b.*$", "", line).strip(" |•-–,")  # drop dates
        line = re.split(r"\s*(?:[,:|–]| - | at | @ )\s*", line)[0].strip()      # stop at the employer
        if line and line.lower() != (name or "").lower() and 3 < len(line) < 70 and _TITLE_HINT.search(line) \
                and "@" not in line and not re.search(r"\d{3}", line):
            return line
    return None


# --- searching ----------------------------------------------------------------------

def document(c) -> str:
    """Everything searchable about a candidate: profile fields first, then the résumé."""
    head = " · ".join(x for x in [c["name"], c["title"], c["company"],
                                  ", ".join(x for x in [c["city"], c["state"]] if x)] if x)
    return "\n".join(x for x in [head, c["skills"], c["profile"], c["resume_text"]] if x)


# Searchable text per candidate, kept in memory between searches:
# {database file: {candidate id: (version, source, text, lower-cased text)}}
_DOCS: dict[str, dict[int, tuple[str, str, str, str]]] = {}


_DOC_STAMP: dict = {}
_SEARCHES: dict = {}


def _documents(db: DB, source: str | None) -> list[tuple[int, str, str]]:
    cache = _DOCS.setdefault(db.path, {})
    stamp = None if db.conn.in_transaction else _pool(db)["version"]
    if stamp and _DOC_STAMP.get(db.path) == stamp and cache:
        return [(cid, d[2], d[3]) for cid, d in cache.items() if not source or d[1] == source]  # candidates unchanged
    _DOC_STAMP[db.path] = stamp
    versions = {r["id"]: (r["v"], r["source"]) for r in db.conn.execute(
        "SELECT id, source, COALESCE(resume_fetched_at, '') || '|' || COALESCE(updated_at, '') || '|' || "
        "COALESCE(length(skills), 0) || '|' || COALESCE(title, '') || '|' || COALESCE(state, '') AS v FROM candidates")}
    stale = [cid for cid, (v, _) in versions.items() if cid not in cache or cache[cid][0] != v]
    for i in range(0, len(stale), 500):
        chunk = stale[i:i + 500]
        for c in db.conn.execute(f"SELECT * FROM candidates WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            doc = document(c)
            cache[c["id"]] = (versions[c["id"]][0], c["source"], doc, doc.lower())
    for cid in set(cache) - set(versions):
        del cache[cid]
    return [(cid, d[2], d[3]) for cid, d in cache.items() if not source or d[1] == source]


def search(db: DB, query: str, limit: int = 50, source: str | None = None) -> tuple[list[lens.Group], list[dict]]:
    """Best matches first. Someone whose job title matches comes before someone whose résumé
    merely mentions the word (a Controller before an analyst who wrote "domain controller")."""
    # Only a change to the candidate list makes an earlier answer stale (logging a call does not).
    stamp = None if db.conn.in_transaction else _pool(db)["version"]
    key = (db.path, stamp, query, limit, source)
    if stamp and key in _SEARCHES:
        return _SEARCHES[key]
    groups = lens.parse(query)
    titles = {c["id"]: c["title"] for c in _pool(db)["rows"]} if groups else {}
    in_title: dict[int, int] = {}
    scored = []
    for cid, doc, lower in _documents(db, source):
        m = lens.counts(doc, groups, lower) if groups else lens.Match([], [])
        if groups and not m.groups_found:
            continue
        t = titles.get(cid) or ""
        in_title[cid] = lens.counts(t, groups, t.lower()).groups_found if t else 0
        scored.append((cid, m))
    scored.sort(key=lambda x: (-x[1].groups_found, -in_title[x[0]], -x[1].total))
    scored = scored[: limit * 2]
    rows = {}
    ids = [cid for cid, _ in scored]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for c in db.conn.execute(f"SELECT * FROM candidates WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            rows[c["id"]] = dict(c)
    results = []
    for cid, m in scored:
        if cid in rows:
            results.append({"c": rows[cid], "match": m, "fit": lens.fit_label(m, groups)})
    results.sort(key=lambda r: (-r["match"].groups_found, -in_title.get(r["c"]["id"], 0), -r["match"].total,
                                r["c"]["name"] or ""))
    results = results[:limit]
    # Résumés not downloaded yet for the best results: fetch those first.
    for r in reversed(results):
        if r["c"]["source"] == "loxo" and r["c"]["resume_status"] is None:
            RESUME_QUEUE.appendleft(r["c"]["id"])
    if stamp:
        if len(_SEARCHES) > 40:
            _SEARCHES.clear()
        _SEARCHES[key] = (groups, results)
    return groups, results


# --- bridging to companies -------------------------------------------------------------

def summary(c, groups: list[lens.Group] | None = None) -> dict:
    """Anonymised description for pitching: no name, no current employer."""
    years = years_experience(c["resume_text"] or c["profile"] or "")
    where = ", ".join(x for x in [c["city"], c["state"]] if x)
    title = c["title"] or "candidate"
    bits = [title]
    if years:
        bits.append(f"with about {years} years' experience")
    if where:
        bits.append(f"based in {where}")
    text = " ".join(bits)
    if groups:
        found = [g.label for g, h in zip(groups, lens.match(document(c), groups).hits) if h and g.kind != "location"]
        if found:
            text += " (" + ", ".join(found[:3]) + ")"
    return {"candidate_title": title, "candidate_summary": text, "candidate_location": where}


def matching_jobs(db: DB, candidate_id: int, limit: int = 25) -> list[dict]:
    """Open roles whose title shares the key words of the candidate's title, best hiring score first."""
    from .hiringscore import score_all
    from .offlimits import status as offlimits_status
    c = db.conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    if not c or not c["title"]:
        return []
    noise = {"senior", "sr", "junior", "jr", "lead", "staff", "principal", "ii", "iii", "i", "of", "and", "the",
             "associate", "assistant", "head", "chief", "level"}
    words = [w for w in re.findall(r"[a-z]+", c["title"].lower()) if w not in noise and len(w) > 2]
    if not words:
        return []
    pats = [re.compile(r"\b" + lens._word_rx(w), re.I) for w in words]
    from .hiringscore import _location_ok, load_desk
    desk = load_desk(db)
    scores = score_all(db, desk)
    out = []
    for j in db.conn.execute("SELECT j.*, co.name AS company_name, co.key AS company_key, co.city "
                             "FROM jobs j JOIN companies co ON co.id = j.company_id WHERE j.closed_at IS NULL"):
        hits = sum(1 for p in pats if p.search(j["title"]))
        if hits * 2 < len(pats) or hits == 0:
            continue
        if desk.locations and not _location_ok(j["location"], j["city"], desk, j["title"]):
            continue  # outside your area (same rule as the Jobs page)
        st = offlimits_status(db, j["company_id"])
        hm = db.conn.execute(
            "SELECT p.* FROM job_contacts jc JOIN people p ON p.id = jc.person_id WHERE jc.job_id = ? "
            "ORDER BY jc.rank LIMIT 1", (j["id"],)).fetchone()
        out.append({"job": dict(j), "hits": hits, "score": scores.get(j["company_id"]), "offlimits": st,
                    "manager": dict(hm) if hm else None})
    out.sort(key=lambda r: (r["offlimits"].blocked, -r["hits"], -(r["score"].total if r["score"] else 0)))
    return out[:limit]


_ROLE_NOISE = {"senior", "sr", "junior", "jr", "lead", "staff", "principal", "ii", "iii", "iv", "i", "of", "and",
               "the", "associate", "assistant", "level", "manager", "specialist", "consultant", "in", "for",
               "contract", "remote", "onsite", "site", "hybrid", "full", "time", "part"}
_DFW = re.compile(r"\b(dallas|fort worth|ft\.? worth|plano|irving|frisco|richardson|addison|arlington|southlake|"
                  r"westlake|mckinney|allen|carrollton|lewisville|grapevine|coppell|denton|garland|keller|"
                  r"las colinas|flower mound|mesquite|grand prairie|euless|bedford|hurst|prosper|rockwall)\b", re.I)
_MATCH_CACHE: dict[tuple, list[dict]] = {}
STALE_YEARS = 3  # a résumé whose latest date is older than this needs a check-in before pitching


def resume_year(text: str) -> int | None:
    """The latest year a résumé mentions (its 'as of' date): 'March 2015 - Present' -> 2015."""
    this_year = date.today().year
    years = [int(y) for y in re.findall(r"\b(19[89]\d|20\d\d)\b", text or "") if int(y) <= this_year]
    return max(years) if years else None


_LEVELS = [  # checked in order; first match wins
    (6, r"\b(chief|cxo|ceo|cto|cfo|coo|cio|ciso|president|founder|partner)\b"),
    (5, r"\b(vp|svp|evp|vice president|head of)\b"),
    (4, r"\b(director|dir\.?)\b"),
    (3, r"\b(manager|mgr|principal|staff|architect|team lead|lead)\b"),
    (2, r"\b(senior|sr\.?|iii|iv|3|4)\b"),
    (0, r"\b(intern|junior|jr\.?|entry|associate|assistant|trainee|i|1)\b"),
]


def level_of(title: str) -> int:
    """Rough seniority: 0 entry · 1 mid · 2 senior · 3 lead/manager · 4 director · 5 VP · 6 C-level."""
    t = (title or "").lower()
    for lvl, rx in _LEVELS:
        if re.search(rx, t):
            return lvl
    return 1


def _role_words(title: str) -> list[str]:
    from .today import clean_role
    words = [w for w in re.findall(r"[a-z0-9+#]+", clean_role(title).lower()) if w not in _ROLE_NOISE]
    return [w for w in words if len(w) > 1]


_POOL: dict = {}


def _pool(db: DB) -> dict:
    """Every candidate's short details, read once and reused until the candidate list changes.

    Reading the whole candidate table for each open role is what made the Jobs page take seconds.
    """
    stamp = None if db.conn.in_transaction else db.stamp()
    have = _POOL.get(db.path)
    if have and stamp and stamp[0] is not None and have["stamp"] == stamp:
        return have
    version = tuple(db.conn.execute("SELECT COUNT(*), MAX(COALESCE(updated_at, '')), "
                                    "MAX(COALESCE(resume_fetched_at, '')) FROM candidates").fetchone())
    if have and have["version"] == version:
        have["stamp"] = stamp
        return have
    rows = [dict(r) for r in db.conn.execute(
        "SELECT id, name, title, company, city, state, resume_status, resume_text IS NOT NULL "
        "AS has_resume, email, phone FROM candidates WHERE title IS NOT NULL AND title <> ''")]
    _POOL[db.path] = {"stamp": stamp, "version": version, "rows": rows, "years": {}}
    return _POOL[db.path]


def candidates_for_job(db: DB, job_id: int, limit: int = 3) -> list[dict]:
    """Best candidates to pitch for one open role: [{c, why, local}], best first.

    A candidate's own job title must contain every key word of the role (allowing word
    endings: engineer / engineering), or all but one for roles of 3+ key words. Then:
    in DFW > elsewhere in Texas > unknown > out of state, a résumé on file, and their
    title matching more of the role. People who currently work at that company are left out.
    """
    j = db.conn.execute("SELECT j.*, co.name AS company_name FROM jobs j JOIN companies co ON co.id = j.company_id "
                        "WHERE j.id = ?", (job_id,)).fetchone()
    if not j:
        return []
    pool = _pool(db)
    key = (db.path, job_id, pool["version"], limit)
    if key in _MATCH_CACHE:
        return _MATCH_CACHE[key]
    words = _role_words(j["title"])
    if not words:
        return []
    pats = [re.compile(r"\b" + lens._word_rx(w), re.I) for w in words]
    need = len(pats) if len(pats) < 3 else len(pats) - 1
    company = (j["company_name"] or "").lower()
    role_level = level_of(j["title"])
    scored = []
    for c in pool["rows"]:
        hits = sum(1 for p in pats if p.search(c["title"]))
        if hits < need:
            continue
        if abs(level_of(c["title"]) - role_level) > 1:
            continue  # a Director for an individual-contributor role (or the reverse) isn't a fit
        if company and c["company"] and company.split()[0] in c["company"].lower():
            continue  # works there now
        place = " ".join(x for x in [c["city"], c["state"]] if x)
        texas = re.search(r"^(TX|Texas)$", (c["state"] or "").strip(), re.I) or re.search(r"\b(TX|Texas)\b", c["city"] or "")
        if _DFW.search(c["city"] or "") and (texas or not (c["state"] or "").strip()):  # not Arlington, VA
            local, where = 3, "in DFW"
        elif re.search(r"\b(TX|Texas)\b", place, re.I):
            local, where = 2, "in Texas"
        elif not place:
            local, where = 1, "location unknown"
        else:
            local, where = 0, "out of state"
        scored.append(((local, c["has_resume"], hits, bool(c["email"] or c["phone"])), c, where))
    scored.sort(key=lambda x: x[0], reverse=True)
    # Freshness: how recent is the résumé? An old résumé means the title and city may be out of date.
    top = scored[:max(limit * 10, 30)]
    years = pool["years"]
    need_year = [c["id"] for _, c, _ in top if c["id"] not in years]
    if need_year:
        for cid, text in db.conn.execute(
                f"SELECT id, resume_text FROM candidates WHERE id IN ({','.join('?' * len(need_year))})", need_year):
            years[cid] = resume_year(text or "")
    this_year = date.today().year
    ranked = []
    for s, c, where in top:
        yr = years.get(c["id"])
        fresh = 2 if yr and yr >= this_year - STALE_YEARS else (1 if not yr else 0)
        ranked.append(((s[0], fresh, s[2], s[3]), c, where, yr))
    ranked.sort(key=lambda x: x[0], reverse=True)
    out = []
    for s, c, where, yr in ranked[:limit]:
        bits = [c["title"]]
        if c["city"] or c["state"]:
            bits.append(", ".join(x for x in [c["city"], c["state"]] if x))
        bits.append(where if where != "location unknown" else "location not on file")
        stale = s[1] < 2
        if yr and not stale:
            bits.append(f"résumé current ({yr})")
        elif yr:
            bits.append(f"résumé dated {yr}: verify before pitching")
        else:
            bits.append("no résumé on file: verify before pitching")
        out.append({"c": dict(c), "why": " · ".join(bits), "local": s[0] >= 2, "where": where,
                    "stale": stale, "resume_year": yr})
    if len(_MATCH_CACHE) > 2000:
        _MATCH_CACHE.clear()
    _MATCH_CACHE[key] = out
    return out


def candidate_counts_for_job(db: DB, job_id: int) -> int:
    """How many candidates match a role at all (for 'you have N people for this')."""
    j = db.conn.execute("SELECT title FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return len(candidates_for_job(db, job_id, limit=50)) if j else 0


def pitch(db: DB, candidate_id: int, job_id: int, template_id: int | None = None) -> dict:
    """A filled-in candidate pitch email to the job's likely hiring manager."""
    from . import crm, messaging
    crm.ensure_starters(db)
    c = db.conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    j = db.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    hm = db.conn.execute("SELECT p.* FROM job_contacts jc JOIN people p ON p.id = jc.person_id WHERE jc.job_id = ? "
                         "ORDER BY jc.rank LIMIT 1", (job_id,)).fetchone()
    tpl = db.template(template_id) if template_id else next(
        (t for t in db.templates("email") if t["name"] == PITCH_TEMPLATE[0]), None)
    vals = messaging.person_fields(db, hm["id"] if hm else None, j["company_id"])
    from .today import clean_role
    vals["role"] = clean_role(j["title"])  # "Senior Data Engineer", not "…(Contract) (On-site)"
    vals.update(summary(c))
    body, missing = messaging.render(tpl["body"], vals)
    subject, _ = messaging.render(tpl["subject"] or "", vals)
    return {"to": hm["email"] if hm else None, "person": dict(hm) if hm else None, "subject": subject,
            "body": body + crm.footer(db), "missing": missing, "company_id": j["company_id"], "template_id": tpl["id"]}


PITCH_TEMPLATE = (
    "Email: candidate intro",
    "{candidate_title} for your {role} opening",
    "Hi {first_name},\n\nI'm working with a {candidate_summary}. With the {role} opening at {company}, "
    "I thought they'd be worth a look.\n\nWould you like me to send over their résumé? I'm happy to set up a "
    "quick call too.\n\nBest,\n{my_name}",
)


_COUNTS: dict = {}


def candidate_counts(db: DB) -> dict:
    """Totals for the Candidates page, recounted only when the candidate list changes."""
    version = _pool(db)["version"]
    if _COUNTS.get(db.path, (None,))[0] != version:
        _COUNTS[db.path] = (version, _candidate_counts(db))
    return _COUNTS[db.path][1]


def _candidate_counts(db: DB) -> dict:
    one = lambda q: db.conn.execute(q).fetchone()[0]  # noqa: E731
    return {"total": one("SELECT COUNT(*) FROM candidates"),
            "with_resume": one("SELECT COUNT(*) FROM candidates WHERE resume_text IS NOT NULL"),
            "loxo": one("SELECT COUNT(*) FROM candidates WHERE source = 'loxo'"),
            "uploaded": one("SELECT COUNT(*) FROM candidates WHERE source = 'upload'"),
            "backlog": resume_backlog(db)}
