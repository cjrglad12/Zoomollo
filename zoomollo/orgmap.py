"""Turn job titles into department + seniority, find people via search
results, and match open roles to likely hiring managers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from .extract import looks_like_name
from .search import SearchProvider

SENIORITY_RANK = {"c_level": 5, "vp": 4, "director": 3, "manager": 2, "staff": 1}

_SENIORITY = [
    ("vp", r"\b(vp|svp|evp|avp|vice[\s-]president)\b"),
    ("c_level", r"\b(ceo|cfo|coo|cto|cmo|cio|cro|chro|cpo|chief|founder|co-?founder|(?<!product )owner|"
                r"president|managing partner|managing director|senior partner|founding partner|"
                r"name partner|equity partner|managing principal)\b"
                r"(?!\s+(manager|success|marketing|relations|development|engineer|specialist|coordinator|"
                r"account|program|enablement|operations))|^(partner|principal)\s*($|,|&|\band\b|\bat\b|-)"),
    ("director", r"\b(director|head of|head,|head\b)"),
    ("manager", r"\b(manager|mgr|lead|leader|supervisor|superintendent|foreman|team lead|"
                r"general manager)\b"),
]

# Checked in order; first match wins.
_DEPARTMENTS = [
    ("executive", r"\b(ceo|chief executive|founder|co-?founder|(?<!product )owner|(?<!vice )president|managing partner|"
                  r"managing director|general manager)\b"),
    ("hr", r"\b(hr|human resources|people|talent|recruit\w*|chro|staffing)\b"),
    ("sales", r"\b(sales|account executive|business development|bdr|sdr|revenue|cro|"
              r"account manager|estimator)\b"),
    ("marketing", r"\b(marketing|brand|growth|content|seo|communications|cmo|demand gen)\b"),
    ("finance", r"\b(finance|financial|accounting|accountant|controller|cfo|bookkeep\w*|payroll|treasur\w*)\b"),
    ("customer", r"\b(customer success|customer service|support|client services|account management)\b"),
    ("product", r"\b(product|ux|ui|design|designer)\b"),
    ("it", r"\b(it|information technology|systems administrator|cio|network|helpdesk|infrastructure)\b"),
    ("engineering", r"\b(engineer\w*|software|developer|devops|cto|technical|architect|data)\b"),
    ("legal", r"\b(legal|counsel|attorney|lawyer|paralegal|compliance)\b"),
    ("operations", r"\b(operations|coo|logistics|supply chain|project manager|superintendent|field|"
                   r"service|dispatch|warehouse|facilities|production|plant|install\w*|technician|"
                   r"maintenance|construction|foreman)\b"),
]


def seniority(title: str | None) -> str:
    t = (title or "").lower()
    for level, rx in _SENIORITY:
        if re.search(rx, t):
            return level
    return "staff"


def department(title: str | None) -> str:
    t = (title or "").lower()
    for dept, rx in _DEPARTMENTS:
        if re.search(rx, t):
            return dept
    return "other"


_SUFFIX = re.compile(r"\b(inc|llc|l\.l\.c|ltd|corp|corporation|co|company|group|the|pllc|pc|lp|llp)\b\.?")


def norm_company(name: str) -> str:
    s = _SUFFIX.sub(" ", (name or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def mentions(company: str, text: str) -> bool:
    c = norm_company(company)
    return bool(c) and f" {c} " in f" {norm_company(text)} "


@dataclass
class Found:
    name: str
    title: str | None
    url: str
    snippet: str = ""


def parse_profile_result(title: str, url: str, snippet: str, company: str) -> Found | None:
    """Parse a public-profile search result like
    'Jane Doe - VP of Sales - Acme Corp | LinkedIn'. Only results that
    clearly name the company are kept."""
    if "linkedin.com/in/" not in url:
        return None
    t = re.sub(r"\s*[|·]\s*LinkedIn.*$", "", title).strip()
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", t) if p.strip()]
    if not parts:
        return None
    name = re.split(r",", parts[0])[0].strip()  # "Jane Doe, CPA"
    if not looks_like_name(name):
        return None
    job_title = None
    rest = parts[1:]
    if len(rest) >= 2:
        job_title, comp = rest[0], " ".join(rest[1:])
    elif len(rest) == 1:
        if " at " in rest[0]:
            job_title, comp = rest[0].split(" at ", 1)
        elif mentions(company, rest[0]):
            comp = rest[0]
        else:
            job_title, comp = rest[0], ""
    else:
        comp = ""
    if not (mentions(company, comp) or mentions(company, job_title or "") or mentions(company, snippet)):
        return None
    if job_title and mentions(company, job_title) and " at " in job_title:
        job_title = job_title.split(" at ")[0]
    return Found(name=name, title=(job_title or None), url=url.split("?")[0], snippet=snippet)


def search_people(provider: SearchProvider, company: str, city: str | None = None,
                  departments: list[str] | None = None, max_queries: int = 4) -> list[Found]:
    """X-ray search: query a search engine for public profiles at a company."""
    queries = [
        f'site:linkedin.com/in "{company}" (owner OR president OR CEO OR founder OR VP OR director)',
        f'site:linkedin.com/in "{company}" (manager OR "head of" OR lead OR superintendent)',
        # Recruiting / talent people are excluded later; never searched for.
    ]
    if city:
        queries.append(f'site:linkedin.com/in "{company}" "{city.split(",")[0]}"')
    for d in departments or []:
        if d not in ("other", "executive"):
            queries.append(f'site:linkedin.com/in "{company}" {d} (manager OR director OR head OR VP)')
    found: dict[str, Found] = {}
    for q in queries[:max_queries]:
        for r in provider.search(q):
            f = parse_profile_result(r.title, r.url, r.snippet, company)
            if f and f.name.lower() not in found:
                found[f.name.lower()] = f
    return list(found.values())


def hiring_managers(people: list[dict], jobs: list[dict]) -> dict[int, list[dict]]:
    """For each job id, the people most likely to own the hire, best first.

    Same department and more senior than the role wins, closest level first.
    Small companies fall back to the owner / executives. Recruiters are never suggested.
    """
    out: dict[int, list[dict]] = {}
    for j in jobs:
        dept, level = department(j["title"]), SENIORITY_RANK[seniority(j["title"])]
        senior = [p for p in people if SENIORITY_RANK.get(p["seniority"], 1) > max(level, 1)]
        same = sorted((p for p in senior if p["department"] == dept and dept != "other"),
                      key=lambda p: SENIORITY_RANK.get(p["seniority"], 1))
        picks = same[:2]
        if not picks:
            execs = [p for p in people if p["department"] == "executive" or p["seniority"] == "c_level"]
            picks = sorted(execs, key=lambda p: -SENIORITY_RANK.get(p["seniority"], 1))[:1]
        out[j["id"]] = picks
    return out


def domain_of(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")
