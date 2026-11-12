"""Import a call list from a CSV (Apollo, ZoomInfo, Sales Navigator, Loxo, or your own sheet).

1. read(): decode the file and guess which column is which.
2. You confirm the columns on screen.
3. apply(): match each row to a company and person already in Zoomollo (website / email
   domain, then company name; person by name at that company) or add them. Rows for
   staffing firms you've blocked, off-limits clients and accounts marked Not interested
   are left out of the call list. Existing details are kept; only blanks are filled.
"""

from __future__ import annotations

import csv
import io
import re

from .db import DB

MAX_ROWS = 5000

# field -> (label, header pattern)
FIELDS = {
    "company": ("Company", r"^(company|company name|account|account name|organization|organisation|employer|firm|"
                           r"business|business name|current company)$"),
    "website": ("Website", r"(website|web site|domain|url|company url|homepage)"),
    "full_name": ("Full name", r"^(name|full name|contact|contact name|person|lead name)$"),
    "first_name": ("First name", r"^(first|first name|firstname|given name)$"),
    "last_name": ("Last name", r"^(last|last name|lastname|surname|family name)$"),
    "title": ("Job title", r"(title|job title|position|role|headline)"),
    "phone": ("Direct phone", r"(direct|work direct|direct dial|phone$|^phone number|work phone|office phone|"
                              r"^telephone|^tel$)"),
    "mobile": ("Mobile", r"(mobile|cell)"),
    "company_phone": ("Company phone", r"(company phone|corporate phone|main|hq phone|headquarters phone|"
                                       r"switchboard|business phone)"),
    "email": ("Email", r"^(?!.*\b(name|status|opt|source|confidence|verified|type|domain)\b).*\be-?mails?\b"),
    "city": ("City", r"^(city|company city|location|contact city|metro)$"),
    "state": ("State", r"^(state|company state|region|province|st)$"),
    "linkedin": ("LinkedIn", r"linkedin"),
}
FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com", "me.com",
             "live.com", "msn.com", "comcast.net", "att.net", "sbcglobal.net", "verizon.net", "ymail.com",
             "protonmail.com", "proton.me", "gmx.com", "mail.com", "bellsouth.net", "cox.net", "charter.net"}


def read(data: bytes) -> tuple[list[str], list[list[str]]]:
    """(headers, rows) from an uploaded file."""
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sample = text[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        return [], []
    headers = [h.strip() or f"Column {i + 1}" for i, h in enumerate(rows[0])]
    body = [(r + [""] * len(headers))[:len(headers)] for r in rows[1:MAX_ROWS + 1]]
    return headers, body


def guess(headers: list[str]) -> dict[str, int | None]:
    """field -> column index. Specific patterns win (e.g. 'Company Phone' isn't the direct phone)."""
    low = [h.lower().strip() for h in headers]
    out: dict[str, int | None] = {}
    used: set[int] = set()
    # Most specific first so 'company phone' / 'mobile' / 'company' claim their columns before 'phone'.
    for field in ("linkedin", "email", "company_phone", "mobile", "first_name", "last_name", "full_name", "website",
                  "company", "title", "phone", "city", "state"):
        rx = re.compile(FIELDS[field][1], re.I)
        hit = next((i for i, h in enumerate(low) if i not in used and rx.search(h)), None)
        if field == "title":  # 'Job Title' but not 'Title Level' style extras when a better one exists
            exact = next((i for i, h in enumerate(low) if i not in used and h in ("title", "job title")), None)
            hit = exact if exact is not None else hit
        if field == "website" and hit is not None and "linkedin" in low[hit]:
            hit = None
        out[field] = hit
        if hit is not None:
            used.add(hit)
    return out


def norm_phone(s: str) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    m = re.search(r"(?i)\s*(?:x|ext\.?|extension)\s*(\d+)\s*$", s)
    ext = m.group(1) if m else None
    main = s[:m.start()] if m else s
    digits = re.sub(r"\D", "", main)
    if main.strip().startswith("+") and len(digits) >= 8:
        out = "+" + digits
    elif len(digits) == 10:
        out = "+1" + digits
    elif len(digits) == 11 and digits[0] == "1":
        out = "+" + digits
    else:
        return None
    return f"{out} x{ext}" if ext else out


def _domain(website: str, email: str) -> str | None:
    from .crawler import normalize_domain
    w = (website or "").strip()
    if w and "linkedin.com" not in w.lower():
        try:
            d = normalize_domain(w)
            if d and "." in d:
                return d
        except Exception:  # noqa: BLE001
            pass
    if "@" in (email or ""):
        d = email.strip().lower().rsplit("@", 1)[1]
        if d not in FREE_MAIL and "." in d:
            return d
    return None


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]


def apply(db: DB, headers: list[str], rows: list[list[str]], mapping: dict[str, int | None],
          filename: str = "CSV", what: str = "", default_city: str = "") -> dict:
    from . import today as td
    from .offlimits import status as offlimits_status
    from .orgmap import norm_company
    get = lambda r, f: (r[mapping[f]].strip() if mapping.get(f) is not None and mapping[f] < len(r) else "")  # noqa: E731
    by_name = {}
    for c in db.conn.execute("SELECT id, name FROM companies WHERE name IS NOT NULL"):
        by_name.setdefault(norm_company(c["name"]), c["id"])
    stages = {r[0]: r[1] for r in db.conn.execute("SELECT company_id, stage FROM pipeline")}
    res = {"rows": len(rows), "companies_new": 0, "companies_matched": 0, "people_new": 0, "people_matched": 0,
           "skipped": [], "items": [], "new_domains": []}
    seen_items, seen_new = set(), set()
    source = f"imported from {filename}"
    for n, r in enumerate(rows, start=2):  # row 1 is the header, so numbers match the spreadsheet
        company = get(r, "company")
        email = get(r, "email").lower()
        if not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[a-z]{2,}", email):
            email = ""  # blank, "N/A", or a column that isn't really email
        dom = _domain(get(r, "website"), email)
        if not company and not dom:
            res["skipped"].append((n, "no company or website"))
            continue
        if db.is_blocked(dom) if dom else False:
            res["skipped"].append((n, f"{company or dom}: on your blocked list (staffing / recruiting firm)"))
            continue
        cid = None
        if dom:
            row = db.conn.execute("SELECT id FROM companies WHERE key = ? OR domain = ?", (dom, dom)).fetchone()
            cid = row[0] if row else None
        if cid is None and company:
            cid = by_name.get(norm_company(company))
        city = ", ".join(x for x in [get(r, "city"), get(r, "state")] if x) or default_city or None
        if cid is None:
            key = dom or f"import:{_slug(company)}"
            cid = db.company(key, domain=dom, name=company or None)
            if cid not in seen_new:
                seen_new.add(cid)
                res["companies_new"] += 1
                if dom:
                    res["new_domains"].append(dom)
            db.update_company(cid, industry=what or None, city=city, source=source)
            if company:
                by_name[norm_company(company)] = cid
        elif cid not in seen_new:
            seen_new.add(cid)
            res["companies_matched"] += 1
        co = db.company_row(cid)
        fill = {}
        if city and not co["city"]:
            fill["city"] = city
        if company and not co["name"]:
            fill["name"] = company
        cphone = norm_phone(get(r, "company_phone"))
        if cphone and not db.is_suppressed(cphone):
            if not co["phone"]:
                fill["phone"] = cphone
            db.add_contact(cid, "phone", cphone, "company", source_url=source)
        db.update_company(cid, **fill)

        name = get(r, "full_name") or " ".join(x for x in [get(r, "first_name"), get(r, "last_name")] if x)
        name = " ".join(name.split())
        title = get(r, "title") or None
        phone = norm_phone(get(r, "phone")) or norm_phone(get(r, "mobile"))
        if phone and db.is_suppressed(phone):
            phone = None
        pid = None
        if name and len(name) > 1:
            existed = db.conn.execute("SELECT id FROM people WHERE company_id = ? AND name_key = ?",
                                      (cid, name.lower())).fetchone()
            li = get(r, "linkedin")
            pid = db.add_person(cid, name, title, email=email or None, phone=phone,
                                linkedin_url=li if "linkedin.com" in li.lower() else None,
                                source="import", source_url=source)
            if pid is None:
                res["skipped"].append((n, f"{name}: recruiting / talent-acquisition title, not kept"))
            elif existed:
                res["people_matched"] += 1
            else:
                res["people_new"] += 1
            if pid and email:
                db.add_contact(cid, "email", email, "person", name=name, title=title, source_url=source)
            if pid and phone:
                db.add_contact(cid, "phone", phone, "person", name=name, title=title, source_url=source)
        db.commit()

        # Should this row be on the call list?
        label = co["name"] or company or co["key"]
        if offlimits_status(db, cid).blocked:
            res["skipped"].append((n, f"{label}: off-limits"))
            continue
        if stages.get(cid) in ("not_now", "lost"):
            res["skipped"].append((n, f"{label}: marked Not interested / lost"))
            continue
        prow = db.conn.execute("SELECT name, phone FROM people WHERE id = ?", (pid,)).fetchone() if pid else None
        dial = (prow["phone"] if prow and prow["phone"] else None)
        kind = "direct" if dial else "main line"
        dial = dial or td._main_line(db, cid, db.company_row(cid)["phone"])
        if not dial:
            res["skipped"].append((n, f"{label}{' / ' + name if name else ''}: no phone number"))
            continue
        k = (cid, pid)
        if k in seen_items:
            continue
        seen_items.add(k)
        res["items"].append({"company_id": cid, "person_id": pid, "company": label,
                             "person": prow["name"] if prow else None, "phone": dial, "kind": kind})
    return res
