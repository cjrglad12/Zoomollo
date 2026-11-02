"""Candidates: from Loxo or uploaded résumés, searched through the keyword lens,
and bridged to companies hiring for what they do.

Loxo: the candidate list (title, company, location, skills, tags, contact
details) syncs in a couple of minutes. Résumés are downloaded gradually in the
background (two requests each), and candidates matching your current search
jump the queue. Everything is stored on this computer.
"""

from __future__ import annotations

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


def _documents(db: DB, source: str | None) -> list[tuple[int, str, str]]:
    cache = _DOCS.setdefault(db.path, {})
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
    groups = lens.parse(query)
    scored = []
    for cid, doc, lower in _documents(db, source):
        m = lens.counts(doc, groups, lower) if groups else lens.Match([], [])
        if groups and not m.groups_found:
            continue
        scored.append((cid, m))
    scored.sort(key=lambda x: (-x[1].groups_found, -x[1].total))
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
    results.sort(key=lambda r: (-r["match"].groups_found, -r["match"].total, r["c"]["name"] or ""))
    results = results[:limit]
    # Résumés not downloaded yet for the best results: fetch those first.
    for r in reversed(results):
        if r["c"]["source"] == "loxo" and r["c"]["resume_status"] is None:
            RESUME_QUEUE.appendleft(r["c"]["id"])
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
    scores = score_all(db)
    out = []
    for j in db.conn.execute("SELECT j.*, co.name AS company_name, co.key AS company_key, co.city "
                             "FROM jobs j JOIN companies co ON co.id = j.company_id WHERE j.closed_at IS NULL"):
        hits = sum(1 for p in pats if p.search(j["title"]))
        if hits * 2 < len(pats) or hits == 0:
            continue
        st = offlimits_status(db, j["company_id"])
        hm = db.conn.execute(
            "SELECT p.* FROM job_contacts jc JOIN people p ON p.id = jc.person_id WHERE jc.job_id = ? "
            "ORDER BY jc.rank LIMIT 1", (j["id"],)).fetchone()
        out.append({"job": dict(j), "hits": hits, "score": scores.get(j["company_id"]), "offlimits": st,
                    "manager": dict(hm) if hm else None})
    out.sort(key=lambda r: (r["offlimits"].blocked, -r["hits"], -(r["score"].total if r["score"] else 0)))
    return out[:limit]


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
    vals["role"] = j["title"]
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


def candidate_counts(db: DB) -> dict:
    one = lambda q: db.conn.execute(q).fetchone()[0]  # noqa: E731
    return {"total": one("SELECT COUNT(*) FROM candidates"),
            "with_resume": one("SELECT COUNT(*) FROM candidates WHERE resume_text IS NOT NULL"),
            "loxo": one("SELECT COUNT(*) FROM candidates WHERE source = 'loxo'"),
            "uploaded": one("SELECT COUNT(*) FROM candidates WHERE source = 'upload'"),
            "backlog": resume_backlog(db)}
