"""Public data files as a company source: license lists, permit records,
business registrations. Read straight from the government site or a file
you downloaded; no outside service in between.

Works with:
  * CSV files (a link or a file on your computer)
  * JSON lists, including city/state open-data "resource" links
    (e.g. https://data.<city>.gov/resource/abcd-1234.json)

Columns are recognised by their names (business name, phone, email, website,
address, city, license type, owner...). Rows are kept when they mention the
industry you're prospecting and, if there's a city column, the city.
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from urllib.parse import urlencode, urlparse

import requests

from .crawler import USER_AGENT, normalize_domain
from .discover import STATES, Candidate, is_company_domain, keyword_regex, split_where

MAX_BYTES = 300 * 1024 * 1024

# Column name patterns, most specific first.
COLUMNS = {
    "name": r"^(business|company|firm|contractor|organization|entity|dba|doing_business_as|trade)[ _]*(name)?$|"
            r"^(contractor|business|company|licensee|applicant)_?(company)?_?name$|^name$|^business$",
    "phone": r"phone|telephone|^tel$",
    "email": r"e-?mail",
    "website": r"web ?site|^url$|homepage",
    "address": r"^(street|mailing|business|physical|contractor)?_?address(_?1| line 1)?$|^street$",
    "city": r"^(business_|mailing_|contractor_|physical_)?city$",
    "state": r"^(business_|mailing_|contractor_|physical_)?state$",
    "kind": r"license_?type|license_?class|^type$|category|classification|trade|occupation|naics|"
            r"industry|work_?class|permit_?type|description|specialty|profession",
    "contact": r"owner|qualif|principal|contact_?name|responsible|licensee_?name|^person|contractor_full_name",
}


def _match_columns(headers: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for field, rx in COLUMNS.items():
        for h in headers:
            norm = h.strip().lower().replace(" ", "_")
            if re.search(rx, norm) and h not in out.values():
                out[field] = h
                break
    return out


def _socrata(url: str, what: str) -> str:
    """Open-data 'resource' links accept a full-text filter and a row limit."""
    if "/resource/" in url and "?" not in url:
        words = [w for w in re.findall(r"[a-z0-9]+", what.lower()) if len(w) > 2]
        q = max(words, key=len) if words else ""
        return url + "?" + urlencode({"$limit": 50000, **({"$q": q} if q else {})})
    return url


def load_rows(source: str, what: str = "") -> list[dict]:
    """Rows from a URL or local path (CSV or JSON)."""
    path = Path(source).expanduser()
    if path.is_file():
        raw = path.read_bytes()[:MAX_BYTES]
        kind = "json" if path.suffix.lower() == ".json" else "csv"
    else:
        url = _socrata(source, what)
        r = requests.get(url, timeout=120, stream=True, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        buf = bytearray()
        for chunk in r.iter_content(1 << 16):
            buf += chunk
            if len(buf) > MAX_BYTES:
                break
        raw = bytes(buf)
        ctype = r.headers.get("Content-Type", "")
        kind = "json" if ("json" in ctype or urlparse(url).path.endswith(".json")) else "csv"
    text = raw.decode("utf-8-sig", errors="replace")
    if kind == "json" or text.lstrip()[:1] in "[{":
        data = json.loads(text)
        if isinstance(data, dict):  # {"data": [...]} style wrappers
            data = next((v for v in data.values() if isinstance(v, list)), [])
        return [d for d in data if isinstance(d, dict)]
    return list(csv.DictReader(io.StringIO(text)))


def candidates_from_rows(rows: list[dict], what: str, where: str, label: str) -> list[Candidate]:
    if not rows:
        return []
    cols = _match_columns(list(rows[0].keys()))
    if "name" not in cols:
        return []
    rx = keyword_regex(what)
    kw = re.compile(rf"\b({rx})", re.I) if rx else None
    city, state = split_where(where)
    out: dict[str, Candidate] = {}
    counts: dict[str, int] = {}

    def val(row, field):
        v = row.get(cols[field]) if field in cols else None
        if isinstance(v, dict):  # some JSON feeds nest address/phone objects
            v = " ".join(str(x) for x in v.values() if x)
        return str(v).strip() if v not in (None, "") else None

    for row in rows:
        name = val(row, "name")
        if not name:
            continue
        haystack = " ".join(str(v) for v in row.values() if isinstance(v, (str, int, float)))
        if kw and not kw.search(haystack):
            continue
        row_city = val(row, "city")
        if row_city and city and row_city.lower() != city.lower():
            continue
        row_state = val(row, "state")
        if row_state and state and row_state.upper() != state and row_state.lower() != STATES[state].lower():
            continue
        site = val(row, "website")
        domain = normalize_domain(site) if site and "." in site else None
        if domain and not is_company_domain(domain):
            domain = None
        addr = " ".join(x for x in [val(row, "address"), row_city, row_state] if x) or None
        c = Candidate(name=name.title() if name.isupper() else name, domain=domain,
                      phone=val(row, "phone"), email=val(row, "email"), address=addr, sources={label},
                      hint=True)
        contact = val(row, "contact")
        if contact and contact.lower() != name.lower():
            c.contact_name = contact.title() if contact.isupper() else contact
            c.contact_title = "Owner / license holder"
        counts[c.key] = counts.get(c.key, 0) + 1
        if c.key not in out:
            out[c.key] = c
        else:  # same business again (e.g. another permit): fill gaps
            cur = out[c.key]
            for f in ("phone", "email", "address", "domain", "contact_name", "contact_title"):
                if not getattr(cur, f) and getattr(c, f):
                    setattr(cur, f, getattr(c, f))
    # Businesses on many records (e.g. many permits this year) are the most active: list them first.
    ranked = sorted(out.values(), key=lambda c: -counts[c.key])
    for c in ranked:
        if counts[c.key] > 1:
            c.sources = {f"{label} ({counts[c.key]} records)"}
    return ranked


def from_source(source: str, what: str, where: str, label: str | None = None) -> list[Candidate]:
    label = label or (urlparse(source).netloc or Path(source).name)
    return candidates_from_rows(load_rows(source, what), what, where, label)
