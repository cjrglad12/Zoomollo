"""Cross-reference a company's contacts with its open roles.

For each job, contacts are ranked by how strong the link is:
  1. named in posting   - the person's email, phone or name appears in the job post
  2. reports to         - the post says "reports to the <title>" and someone has that title
  3. contact in posting - an email/phone in the post that isn't tied to a known person
  4. likely hiring mgr  - closest more-senior person in the same department
  5. owner / exec       - small company, no department match

Generic inboxes (careers@, info@...) and recruiting/staffing people are never used.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .db import DB
from .exclusions import is_excluded_email
from .extract import extract_text
from .orgmap import department, hiring_managers, seniority

_REPORTS_TO = re.compile(
    r"\b(?:reports?|reporting)\s+(?:directly\s+)?(?:in\s+)?to\s+(?:the\s+|our\s+|a\s+)?"
    r"([A-Z][\w&/.' -]{2,60}?)(?=[.,;:()\n]|\s+(?:and|who|with|in|on|to|for)\b|$)", re.I)

RANK = {"named in posting": 1, "reports to": 2, "contact in posting": 3, "likely hiring manager": 4,
        "owner / exec": 5}


def _plain(html: str) -> str:
    if "<" in html and ">" in html:
        return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    return html


def reports_to(text: str) -> list[str]:
    return [m.group(1).strip(" ,.") for m in _REPORTS_TO.finditer(text or "")]


def _title_match(wanted: str, title: str | None) -> bool:
    """'Service Manager' vs 'Service Manager, Central Texas' or same dept + level."""
    if not title:
        return False
    w, t = wanted.lower(), title.lower()
    if w in t or t in w:
        return True
    return (department(wanted) == department(title) != "other"
            and seniority(wanted) == seniority(title) != "staff")


def cross_reference(db: DB, company_id: int) -> int:
    """Rebuild job<->contact links for one company. Returns links written."""
    jobs = [dict(j) for j in db.jobs(company_id=company_id, limit=100_000)]
    # A guessed address that shows up in a real job post is proven: upgrade it.
    from .emailcheck import mark_good
    posted = set()
    for j in jobs:
        if j.get("description"):
            posted |= extract_text(_plain(j["description"]))[0]
    for e in posted - {e for e in posted if is_excluded_email(e)}:
        if db.conn.execute("SELECT 1 FROM people WHERE company_id = ? AND email = ? AND email_status = 'guessed'",
                           (company_id, e)).fetchone():
            mark_good(db, e)
            db.conn.execute("UPDATE people SET email_status = 'found' WHERE email = ?", (e,))
    people = [dict(p) for p in db.people(company_id)]
    by_email = {p["email"]: p for p in people if p["email"]}
    by_phone = {p["phone"]: p for p in people if p["phone"]}

    db.conn.execute("DELETE FROM job_contacts WHERE job_id IN (SELECT id FROM jobs WHERE company_id = ?)",
                    (company_id,))
    db.conn.execute("UPDATE people SET hiring_for = NULL WHERE company_id = ?", (company_id,))
    hiring_for: dict[int, list[str]] = {}
    written = 0

    for job in jobs:
        links: dict[tuple, str] = {}  # (person_id, value) -> reason (strongest kept)

        def link(pid, value, reason):
            key = (pid, None if pid else value)
            if key not in links or RANK[reason] < RANK[links[key]]:
                links[key] = reason

        text = _plain(job.get("description") or "")
        if text:
            emails, phones = extract_text(text)
            for e in emails:
                if is_excluded_email(e):
                    continue
                p = by_email.get(e)
                link(p["id"] if p else None, e, "named in posting" if p else "contact in posting")
            for ph in phones:
                p = by_phone.get(ph)
                link(p["id"] if p else None, ph, "named in posting" if p else "contact in posting")
            low = text.lower()
            for p in people:
                if len(p["name"]) > 5 and p["name"].lower() in low:
                    link(p["id"], None, "named in posting")
            for wanted in reports_to(text):
                for p in people:
                    if _title_match(wanted, p["title"]):
                        link(p["id"], None, "reports to")

        dept = department(job["title"])
        for p in hiring_managers(people, [job]).get(job["id"], []):
            if p["department"] == dept and dept != "other":
                reason = "likely hiring manager"
            else:
                reason = "owner / exec"
            link(p["id"], None, reason)

        for (pid, value), reason in links.items():
            db.conn.execute("INSERT OR IGNORE INTO job_contacts (job_id, person_id, value, reason, rank) "
                            "VALUES (?, ?, ?, ?, ?)", (job["id"], pid, value, reason, RANK[reason]))
            written += 1
            if pid:
                hiring_for.setdefault(pid, []).append(job["title"])

    for pid, titles in hiring_for.items():
        uniq = list(dict.fromkeys(titles))
        text = "; ".join(uniq[:5]) + (f" (+{len(uniq) - 5} more)" if len(uniq) > 5 else "")
        db.conn.execute("UPDATE people SET hiring_for = ? WHERE id = ?", (text, pid))
    db.commit()
    return written


def hiring_rows(db: DB, q: str = "", company_id: int | None = None, limit: int = 100_000):
    """Flat job x contact rows for the Hiring view and CSV export."""
    sql = ("SELECT j.id AS job_id, j.title AS job_title, j.location, j.url AS job_url, j.board, "
           "co.id AS company_id, co.name AS company_name, co.key AS company_key, co.domain, co.city, "
           "jc.reason, jc.rank, jc.value, p.name, p.title, p.email, p.email_status, p.phone, p.linkedin_url "
           "FROM jobs j JOIN companies co ON co.id = j.company_id "
           "LEFT JOIN job_contacts jc ON jc.job_id = j.id LEFT JOIN people p ON p.id = jc.person_id WHERE 1=1")
    args: list = []
    if company_id is not None:
        sql += " AND co.id = ?"
        args.append(company_id)
    if q:
        sql += (" AND (lower(j.title) LIKE ? OR lower(COALESCE(co.name, co.key)) LIKE ? "
                "OR lower(COALESCE(co.city,'')) LIKE ? OR lower(COALESCE(p.name,'')) LIKE ?)")
        args += [f"%{q.lower()}%"] * 4
    sql += " ORDER BY co.name, j.title, jc.rank LIMIT ?"
    args.append(limit)
    return db.conn.execute(sql, args).fetchall()


__all__ = ["cross_reference", "hiring_rows", "reports_to"]
