"""Turn job titles into department + seniority, and match open roles to
likely hiring managers."""

from __future__ import annotations

import re
from urllib.parse import urlparse

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
    ("finance", r"\b(finance|financial|accounting|accountant|controller|cfo|bookkeep\w*|payroll|treasur\w*|"
                r"tax|taxation|audit|auditor|assurance|cpa|reconcil\w*|fp&a|accounts payable|accounts receivable)\b"),
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


def linkedin_search_url(name: str, company: str | None = None) -> str:
    """LinkedIn's own people search, filled in. Opened in your browser; nothing is fetched by Zoomollo."""
    from urllib.parse import quote
    keywords = " ".join(x for x in [name, company] if x)
    return "https://www.linkedin.com/search/results/people/?keywords=" + quote(keywords)


def clean_linkedin_url(url: str) -> str | None:
    """Accept only public profile links: https://www.linkedin.com/in/<handle>."""
    import re
    m = re.search(r"linkedin\.com/in/([A-Za-z0-9_\-%]+)", url or "")
    return f"https://www.linkedin.com/in/{m.group(1)}" if m else None
