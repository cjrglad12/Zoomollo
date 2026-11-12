"""Pull emails, phone numbers, people and social links out of HTML or text."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlparse

import phonenumbers
from bs4 import BeautifulSoup

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}\b")

# "jane [at] acme [dot] com", "jane(at)acme(dot)com", "jane AT acme DOT com"
_AT = re.compile(r"\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|\s+AT\s+)\s*", re.I)
_DOT = re.compile(r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|\s+DOT\s+)\s*", re.I)

# Things that look like emails but aren't contacts.
_JUNK_DOMAINS = {
    "example.com", "example.org", "domain.com", "email.com", "yourdomain.com",
    "sentry.io", "sentry-next.wixpress.com", "wixpress.com", "w3.org",
    "schema.org", "godaddy.com", "company.com", "yourcompany.com",
}
_JUNK_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
_NOREPLY = re.compile(r"^(no-?reply|do-?not-?reply|mailer-daemon|bounce)", re.I)

ROLE_LOCALPARTS = {
    "info", "sales", "support", "help", "hello", "hi", "contact", "contactus",
    "admin", "office", "team", "careers", "jobs", "recruiting", "recruitment",
    "talent", "hr", "people", "press", "media", "marketing", "billing",
    "accounts", "finance", "legal", "privacy", "security", "partners",
    "partnerships", "service", "customerservice", "enquiries", "inquiries",
    "webmaster", "feedback", "orders", "general", "hiring",
}

SOCIAL_HOSTS = {
    "linkedin.com": "linkedin",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "github.com": "github",
    "youtube.com": "youtube",
    "tiktok.com": "tiktok",
}

# Links / embeds that reveal which applicant-tracking system a company uses.
ATS_RE = [
    ("greenhouse", re.compile(r"(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=|v1/boards/)?([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/(?:api/v\d/widget/accounts/)?([A-Za-z0-9_-]+)", re.I)),
]
_ATS_NOT_SLUGS = {"embed", "api", "v1", "j", "jobs", "js", "static", "assets"}
# Workday: tenant.wdN.myworkdayjobs.com/[en-US/]SiteName -> slug "tenant.wdN/SiteName".
_WORKDAY_RE = re.compile(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", re.I)
_WORKDAY_NOT_SITES = {"wday", "job", "jobs", "login", "assets", "static"}


def find_ats(text: str) -> set[tuple[str, str]]:
    out = set()
    for board, rx in ATS_RE:
        for m in rx.finditer(text or ""):
            slug = m.group(1).strip("/").lower()
            if slug and slug not in _ATS_NOT_SLUGS:
                out.add((board, slug))
    for m in _WORKDAY_RE.finditer(text or ""):
        tenant, wd, site = m.group(1).lower(), m.group(2).lower(), m.group(3)
        if site.lower() not in _WORKDAY_NOT_SITES:
            out.add(("workday", f"{tenant}.{wd}/{site}"))  # site names are case-sensitive
    for m in _ORACLE_RE.finditer(text or ""):
        out.add(("oracle", f"{m.group(1).lower()}/{m.group(2)}"))
    for m in _ULTIPRO_RE.finditer(text or ""):
        out.add(("ultipro", f"{m.group(1).lower()}/{m.group(2)}/{m.group(3).lower()}"))
    for m in _ICIMS_RE.finditer(text or ""):
        if not m.group(1).lower().startswith(("cdn", "c-", "images", "www", "internal", "login", "community")):
            out.add(("icims", m.group(1).lower() + ".icims.com"))
    for board, rx, skip in _MORE_ATS:
        for m in rx.finditer(text or ""):
            slug = m.group(1).lower()
            if slug not in skip:
                out.add((board, slug))
    return out


_MORE_ATS = [
    ("breezy", re.compile(r"(?<![\w.-])([a-z0-9-]{2,})\.breezy\.hr", re.I), {"app", "www", "gallery-cdn", "assets", "api"}),
    ("jazzhr", re.compile(r"(?<![\w.-])([a-z0-9-]{2,})\.applytojob\.com", re.I), {"app", "www", "assets"}),
    ("jobvite", re.compile(r"jobs\.jobvite\.com/([a-z0-9-]{2,})", re.I), {"careers", "support", "en", "www"}),
    ("clearcompany", re.compile(r"clearcompany\.com/[^\s\"'<>]*?(?:siteId=|/v1/)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                                r"[0-9a-f]{4}-[0-9a-f]{12})", re.I), set()),
    ("adp", re.compile(r"workforcenow\.adp\.com/[^\s\"'<>]*?[?&](?:amp;)?cid=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                       r"[0-9a-f]{4}-[0-9a-f]{12})", re.I), set()),
    ("paylocity", re.compile(r"recruiting\.paylocity\.com/recruiting/jobs/(?:All|List)/([0-9a-f]{8}-[0-9a-f]{4}-"
                             r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I), set()),
]


# Oracle Recruiting Cloud: host.oraclecloud.com/hcmUI/CandidateExperience/en/sites/SiteNumber
_ORACLE_RE = re.compile(r"([a-z0-9-]+\.fa\.[a-z0-9]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-z_-]+/sites/"
                        r"([A-Za-z0-9_]+)", re.I)
# UKG Pro / UltiPro: recruitingN.ultipro.com/TENANT/JobBoard/guid
_ULTIPRO_RE = re.compile(r"(recruiting\d*\.ultipro\.com)/([A-Z0-9]+)/JobBoard/([0-9a-f-]{36})", re.I)
# iCIMS: careers-company.icims.com (not its CDN or tracking hosts)
_ICIMS_RE = re.compile(r"(?<![\w.-])([a-z0-9-]{3,})\.icims\.com", re.I)


# Words that make a short line look like a job title.
_TITLE_HINT = re.compile(
    r"\b(ceo|cfo|coo|cto|cmo|cio|chief|founder|owner|president|partner|principal|director|head|vp|"
    r"vice president|manager|lead|supervisor|superintendent|engineer|officer|recruiter|coordinator|"
    r"specialist|analyst|accountant|attorney|counsel|architect|estimator|controller|associate|"
    r"consultant|administrator|executive|advisor|sales|marketing|operations|hr|talent)\b", re.I)


# Words that make link text look like a job opening (on a careers page).
_JOB_HINT = re.compile(
    r"\b(manager|director|engineer|developer|technician|installer|driver|assistant|associate|"
    r"specialist|coordinator|representative|rep|analyst|accountant|nurse|electrician|plumber|"
    r"apprentice|helper|mechanic|operator|estimator|designer|receptionist|dispatcher|intern|"
    r"supervisor|superintendent|lead|executive|consultant|administrator|clerk|officer|recruiter|"
    r"foreman|planner|scientist|architect|attorney|paralegal|bookkeeper|controller|advisor|agent|"
    r"sales|marketing|cashier|cook|chef|server|teacher|therapist|hygienist|counselor|inspector|"
    r"welder|carpenter|laborer|tech|hvac|warehouse|crew|project)\b", re.I)
_JOB_LINK = re.compile(r"(job|career|position|opening|posting|vacanc|role|apply|opportunit)", re.I)
_NOT_JOB = re.compile(r"^(view|see|browse|search|apply|all|open|current|our|join|careers?|jobs?|"
                      r"job openings|learn more|read more|benefits|culture|life at)\b", re.I)


_NAME_RE = re.compile(r"^[A-Z][a-zA-Z'’-]+(?:\s+[A-Z]\.?)?(?:\s+[A-Z][a-zA-Z'’-]+){1,2}$")


@dataclass
class Person:
    name: str | None = None
    title: str | None = None
    email: str | None = None
    phone: str | None = None
    linkedin: str | None = None


@dataclass
class PageResult:
    url: str
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    people: list[Person] = field(default_factory=list)
    socials: set[str] = field(default_factory=set)
    links: list[str] = field(default_factory=list)
    title: str | None = None
    description: str | None = None  # <meta name="description">
    text: str = ""                   # visible text (first 20k characters)
    ats: set[tuple[str, str]] = field(default_factory=set)
    jobs: list[dict] = field(default_factory=list)
    link_text: dict[str, str] = field(default_factory=dict)  # url -> anchor text
    site_name: str | None = None     # the name the site gives itself (og:site_name / schema.org)
    headcount: int | None = None     # how many people the company says it has


_NUM = r"(\d{1,3}(?:,\d{3})+|\d{2,6})\s*\+?"
_STAFF = (r"(?:full[- ]time\s+)?(?:employees|employee[- ]owners|team members|teammates|professionals|associates|"
          r"staff members|colleagues|people strong|person team|member team)")
# "more than 250 employees", "a team of 300+", "our 180 team members", "450-person firm"
_HEADCOUNT_RE = re.compile(
    rf"(?i)\b(?:team|staff|firm|company|workforce) of (?:more than |over |nearly |about |approximately |almost )?{_NUM}(?!\s*(?:years|offices|locations|%))"
    rf"|\b(?:more than|over|nearly|about|approximately|almost|our|with|employs?|employing|has|have|of)\s+{_NUM}\s*{_STAFF}"
    rf"|\b{_NUM}\s*{_STAFF}\s+(?:across|in|strong|nationwide|worldwide|and growing|company[- ]wide)"
    rf"|\b{_NUM}[- ](?:person|employee|member)\s+(?:firm|company|team)")
_HEADCOUNT_JSONLD = re.compile(r'"numberOfEmployees"\s*:\s*(?:\{[^{}]*?"(?:value|maxValue)"\s*:\s*)?"?(\d{1,3}(?:,\d{3})+|\d{1,6})')


_SUMMARY_LEAD = re.compile(
    r"(?i)^(?:welcome to [^.,:;!]{2,60}?[.,:;!-]\s*|at [^,]{2,40}, (?:we|our)\s+|(?:we|our (?:team|firm|company|mission is to))\s+)")
_SUMMARY_FLUFF = re.compile(
    r"(?i)\b(?:the |a |an )?(?:world[’']?s |nation[’']?s |industry[’']?s )?(?:leading|premier|trusted|award[- ]winning|"
    r"innovative|cutting[- ]edge|world[- ]class|best[- ]in[- ]class|top[- ]rated|full[- ]service|number one|#1)\s+")
_SUMMARY_JUNK = re.compile(r"(?i)(cookie|javascript|browser|log ?in|sign ?in|404|page not found|coming soon|"
                           r"just another wordpress|lorem ipsum|click here|learn more about our)")


def tight_summary(description: str | None, company: str | None = None, limit: int = 110) -> str | None:
    """One short line saying what a company does, from its own homepage description.

    "Welcome to Acme! Acme is the leading provider of payroll software for restaurants. Learn more."
    -> "Provider of payroll software for restaurants"
    """
    t = " ".join((description or "").split())
    if len(t) < 25 or _SUMMARY_JUNK.search(t):
        return None
    t = _SUMMARY_LEAD.sub("", t)
    name = (company or "").strip()
    if name:  # "Acme is ..." / "Acme, Inc. provides ..." -> start at what they do
        t = re.sub(rf"(?i)^{re.escape(name)}(?:,? (?:inc|llc|corp|co|ltd)\.?)?\s*(?:is|are|\||[-–—:])\s+", "", t)
        t = re.sub(rf"(?i)^{re.escape(name)}(?:,? (?:inc|llc|corp|co|ltd)\.?)?\s+(?=provides|offers|delivers|builds|helps|"
                   r"makes|designs|develops|specializes|serves|creates|empowers|enables)", "", t)
    first = re.split(r"(?<=[a-z0-9)])[.!?](?:\s|$)| \| | [–—] ", t)[0].strip(" .-–—|:")
    first = _SUMMARY_FLUFF.sub("", first).strip()
    if len(first) < 15:
        return None
    if len(first) > limit:
        cut = first[:limit].rsplit(" ", 1)[0].rstrip(",;:-–— ")
        cut = re.sub(r"(?i)\s+(?:and|or|for|to|with|of|the|a|an|in|that|which|by|as|on|at|from)$", "", cut)
        first = cut + "…"
    return first[0].upper() + first[1:]


def find_headcount(text: str, raw_html: str = "") -> int | None:
    """How many people a company says it employs, from its own words ("a team of 300+",
    "more than 250 employees") or its structured data. None when it doesn't say."""
    m = _HEADCOUNT_JSONLD.search(raw_html or "")
    if m:
        n = int(m.group(1).replace(",", ""))
        if 2 <= n <= 500000:
            return n
    found = []
    for m in _HEADCOUNT_RE.finditer(text or ""):
        n = int(next(g for g in m.groups() if g).replace(",", ""))
        # Years ("since 1985", "in 2024") and tiny teams ("our 12 team members" in one office) aren't headcount.
        if 20 <= n <= 500000 and not 1900 <= n <= 2100:
            found.append(n)
    return max(found) if found else None


def deobfuscate(text: str) -> str:
    return _DOT.sub(".", _AT.sub("@", text))


def clean_email(raw: str) -> str | None:
    email = unquote(raw).strip().strip(".,;:'\"<>()[]").lower()
    if email.startswith("mailto:"):
        email = email[7:]
    email = email.split("?")[0]
    if not EMAIL_RE.fullmatch(email):
        return None
    local, _, domain = email.rpartition("@")
    if domain in _JUNK_DOMAINS or email.endswith(_JUNK_EXT):
        return None
    if _NOREPLY.match(local):
        return None
    # Hashes / tracking IDs (e.g. 32 hex chars) are not people.
    if re.fullmatch(r"[0-9a-f]{16,}", local):
        return None
    return email


def is_role_email(email: str) -> bool:
    from .exclusions import is_generic_email
    return is_generic_email(email)


def find_emails(text: str) -> set[str]:
    out = set()
    for m in EMAIL_RE.finditer(deobfuscate(text)):
        e = clean_email(m.group(0))
        if e:
            out.add(e)
    return out


def normalize_phone(raw: str, region: str = "US") -> str | None:
    try:
        num = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(num):
        return None
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def find_phones(text: str, region: str = "US") -> set[str]:
    out = set()
    for m in phonenumbers.PhoneNumberMatcher(text, region, leniency=phonenumbers.Leniency.VALID):
        out.add(phonenumbers.format_number(m.number, phonenumbers.PhoneNumberFormat.E164))
    return out


# Website words that look like "Firstname Lastname" in title case but never start a
# person's name ("Explore Your Options", "Case Study", "Payroll Services").
_NOT_FIRST_WORD = set("""
explore our your read open case payroll find self business learn view meet about contact join why how what by for
choose warum pourquoi porque
c-suite csuite c-level executives leaders collaborate build drive deliver transform empower grow innovate
performance similar marketing necessary functional strategic targeting essential analytics advertising
email accelerate account asset cash change claims conversion dedicated demand edi expand family go-live intelligent
large law outsourced threat upload utility weigh workforce tech tapp campaign weekend
who we get request download watch privacy terms cookie menu search back next previous featured latest client
clients executive board home welcome careers career company news all more the see discover schedule book call
sign log apply submit start try free new upcoming recent related popular top best key core my investor investors
press media digital personal financial wealth tax data cloud cyber managed professional global national regional
enterprise industry industries product products service services solutions support resources insights events
leadership management team office offices our success customer customers partner partners case studies study
award awards annual quarterly monthly weekly daily total complete full trusted proven local commercial
""".split())
# Words that don't appear in people's names at all.
_NEVER_IN_NAME = set("""
dropdown positions services solutions options starters story study insights overview message policy resources
platform products industries opportunities benefits culture values mission careers partners clients locations
menu consulting insurance banking mortgage outsourcing team staff dashboard webinar podcast newsletter portal
login video blog ebook whitepaper download overview cookies cookie jobs purpose boldly
roi opening lending campaign snapshot forecast receipts journal adoption claims adjudication conversion toolkit
generation translation privacy reports governance readiness checklist firms connectivity enterprises outsourced
prep engine entrepreneur researcher research upload resume district bypass workforce technology technologies
academy corporate office us impact center centre engineering accounting group inc llc ltd corporation
user users reviewer reviewers review reviews anonymous verified guest management project projects ariba google
yelp facebook aicpa member members questions preparation performance caps community involvement
invoice invoices payment payments billing announces
representative coordinator summary leadership funds surveys syndrome united states employer bundles
infrastructure screening delivered plumbing isd requirement requirements openings handoff validation
optimization provisioning augmentation expertise network
""".split())
# A headline, not a title: "Acme announces Sam Placeholder as new CEO".
_HEADLINE = re.compile(r"(?i)\b(announces?|announced|welcomes?|appoints?|appointed|promotes?|promoted|joins|"
                       r"named|acquires?|celebrates?|as (its )?new)\b")


def plausible_title(title: str | None) -> bool:
    """A job title, not page copy that happened to sit under a heading
    (": Tracking vendor lead times…", "What is a fractional CFO?")."""
    t = (title or "").strip()
    if not t:
        return True  # a name with no title can still be a real person
    # Long executive titles are fine ("Executive Vice President, Clinical Programs and CNO, Dallas Market").
    return (len(t) <= 100 and t[0] not in ":;-–—(" and "?" not in t and "e.g." not in t.lower()
            and "…" not in t and "[...]" not in t
            and not (t.endswith(".") and not t.endswith("...") and len(t.split()) >= 7
                     and not re.search(r"\b(?:[A-Za-z]\.){2,}$", t))
            and not re.search(r"\b(is seeking|are seeking|we are|you will)\b", t, re.I)
            and not _HEADLINE.search(t))


def looks_like_name(text: str | None, company: str | None = None) -> bool:
    """A real person's name, not page text ("Cash Flow Forecast") or the company itself."""
    if not text or "@" in text or not _NAME_RE.match(text.strip()):
        return False
    words = [w.lower().strip("'’.") for w in text.split()]
    parts = [p for w in words for p in w.split("-") if p]  # "Asset-Based Lending" -> asset, based, lending
    if words[0] in _NOT_FIRST_WORD or parts[0] in _NOT_FIRST_WORD or any(p in _NEVER_IN_NAME for p in parts):
        return False
    if company and text.strip().lower() == company.strip().lower():  # "SDO CPA" at SDO CPA
        return False
    if company:  # "Toyota Connected" at Toyota Connected North America
        core = [w for w in re.findall(r"[a-z0-9]+", company.lower()) if len(w) > 3][:2]
        if core and all(w in parts for w in core[:1]) and len(set(parts) & set(core)) >= min(len(core), len(parts)):
            return False
    return True


def social_kind(url: str) -> str | None:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    for h, kind in SOCIAL_HOSTS.items():
        if host == h or host.endswith("." + h):
            return kind
    return None


def _jsonld_people(soup: BeautifulSoup) -> list[Person]:
    people: list[Person] = []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Person" in types:
                email = clean_email(str(node.get("email", ""))) if node.get("email") else None
                phone = normalize_phone(str(node["telephone"])) if node.get("telephone") else None
                name = node.get("name") if isinstance(node.get("name"), str) else None
                title = node.get("jobTitle") if isinstance(node.get("jobTitle"), str) else None
                same = node.get("sameAs")
                same = same if isinstance(same, list) else [same]
                li = next((u for u in same if isinstance(u, str) and "linkedin.com/in/" in u), None)
                if name or email:
                    people.append(Person(name=name, title=title, email=email, phone=phone, linkedin=li))
            for v in node.values():
                if isinstance(v, (list, dict)):
                    walk(v)

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(tag.string or ""))
        except (json.JSONDecodeError, TypeError):
            continue
    return people


def _jsonld_jobs(soup: BeautifulSoup, page_url: str) -> list[dict]:
    """schema.org JobPosting blocks (the markup Google for Jobs reads)."""
    jobs: list[dict] = []

    def loc(j):
        locs = j.get("jobLocation")
        locs = locs if isinstance(locs, list) else [locs]
        out = []
        for l in locs:
            a = (l or {}).get("address") if isinstance(l, dict) else None
            if isinstance(a, dict):
                out.append(", ".join(x for x in [a.get("addressLocality"), a.get("addressRegion")] if x))
            elif isinstance(a, str):
                out.append(a)
        if j.get("jobLocationType") == "TELECOMMUTE":
            out.append("Remote")
        return "; ".join(x for x in out if x) or None

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            t = node.get("@type")
            if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                title = node.get("title") or node.get("name")
                if isinstance(title, str) and title.strip():
                    url = node.get("url") if isinstance(node.get("url"), str) else page_url
                    ident = node.get("identifier")
                    ident = ident.get("value") if isinstance(ident, dict) else ident
                    jobs.append({"title": title.strip()[:200], "url": url, "external_id": str(ident or url),
                                 "location": loc(node), "posted_at": node.get("datePosted"),
                                 "description": node.get("description") or ""})
            for v in node.values():
                if isinstance(v, (list, dict)):
                    walk(v)

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(tag.string or ""))
        except (json.JSONDecodeError, TypeError):
            continue
    return jobs


_OTHER_LANGUAGE = re.compile(r"^/(de|fr|es|it|pt|nl|ja|ko|zh|ru|pl|sv|da|fi|no|tr|ar|hi)(?:[-_][a-zA-Z]{2})?/", re.I)


def _job_links(soup: BeautifulSoup, page_url: str) -> list[dict]:
    """On a careers page, links whose text reads like a role ("HVAC Service Technician")."""
    if not _JOB_LINK.search(urlparse(page_url).path):
        return []
    jobs, seen = [], set()
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        href = urljoin(page_url, a["href"]).split("#")[0]
        if not (4 <= len(text) <= 80) or _NOT_JOB.match(text) or "@" in text:
            continue
        if not _JOB_HINT.search(text) or not _JOB_LINK.search(href) or href.rstrip("/") == page_url.rstrip("/"):
            continue
        if _OTHER_LANGUAGE.match(urlparse(href).path):
            continue  # the same role again on the site's German / French / ... pages
        text = clean_job_link_title(text)
        if href not in seen and len(text) >= 4:
            seen.add(href)
            jobs.append({"title": text, "url": href, "external_id": href, "location": None,
                         "posted_at": None, "description": ""})
    return jobs


_LINK_TAIL = re.compile(r"(?i)\s*[-–—|:]?\s*\b(more details|view details|job details|apply now|apply here|apply|"
                        r"learn more|read more|view job|view role|see details)\s*[»›>→]*\s*$")
_LINK_META = re.compile(r"(?i)\s+(?:full[- ]?time|part[- ]?time|posted\s+\d+\s+\w+\s+ago|posted\s+on\b|posted:)\b.*$")


def clean_job_link_title(text: str) -> str:
    """A careers-page link often carries the whole job card: "Data Scientist More Details",
    "Software Engineer Software Engineer Full Time Plano More Details". Keep just the title."""
    t = re.sub(r"\s+", " ", text or "").strip()
    for _ in range(2):
        t = _LINK_TAIL.sub("", t).strip()
    head = _LINK_META.sub("", t).strip()
    if len(head.split()) >= 2 and not head.endswith("("):  # "Financial Associate (Full Time)" stays whole
        t = head
    words = t.split()
    half = len(words) // 2
    if half and len(words) % 2 == 0 and [w.lower() for w in words[:half]] == [w.lower() for w in words[half:]]:
        t = " ".join(words[:half])  # the title printed twice
    return t.strip(" -–—|:,")


def _card_linkedin(el) -> str | None:
    """A LinkedIn profile link on the same team card as this name (the nearest
    container holding exactly one such link)."""
    node = el
    for _ in range(4):
        node = node.parent
        if node is None or len(node.find_all(el.name)) > 1:
            return None  # reached a container holding several people's cards
        links = {a["href"] for a in node.find_all("a", href=True) if "linkedin.com/in/" in a["href"]}
        if len(links) == 1:
            return links.pop()
        if len(links) > 1:
            return None
    return None


def _team_members(soup: BeautifulSoup) -> list[Person]:
    """Team / leadership cards: a short name line followed closely by a title line."""
    people, seen = [], set()
    for el in soup.find_all(["h2", "h3", "h4", "h5", "h6", "strong", "b", "p", "span", "div", "a"]):
        if el.find(["div", "p", "h2", "h3", "h4", "ul"]):
            continue  # only leaf-ish elements
        name = el.get_text(" ", strip=True)
        if len(name) > 40 or not looks_like_name(name) or _TITLE_HINT.search(name):
            continue
        # Title is usually the next element with text, or text right after a separator.
        title = None
        nxt = el.find_next(string=lambda t: t and t.strip() and t.strip() != name)
        if nxt:
            cand = nxt.strip().strip("|,–—-").strip()
            if 2 < len(cand) < 80 and _TITLE_HINT.search(cand) and "@" not in cand:
                title = cand
        if title and name.lower() not in seen:
            seen.add(name.lower())
            people.append(Person(name=name, title=title, linkedin=_card_linkedin(el)))
    return people


def _profile_person(soup: BeautifulSoup, mailtos: list[str]) -> Person | None:
    """A one-person bio page (/people/jane-doe/): the page heading is their name, their title
    sits under it, and the email link on the page that matches the name is theirs."""
    h1s = soup.find_all("h1")
    if len(h1s) != 1:
        return None
    name = h1s[0].get_text(" ", strip=True)
    if len(name) > 40 or not looks_like_name(name) or _TITLE_HINT.search(name):
        return None
    title = None
    nxt = h1s[0].find_next(string=lambda t: t and t.strip() and t.strip() != name)
    if nxt:
        cand = nxt.strip().strip("|,–—-").strip()
        # The site labels it as the title ("<p class='title'>Survey Technician</p>"): take its word for it.
        labelled = nxt.parent is not None and re.search(r"title|position|role|job", " ".join(nxt.parent.get("class") or []), re.I)
        if 2 < len(cand) < 80 and "@" not in cand and (_TITLE_HINT.search(cand) or (labelled and plausible_title(cand)
                                                                                    and not re.search(r"\d{3}", cand))):
            title = cand
    parts = [re.sub(r"[^a-z]", "", w.lower()) for w in name.split()]
    first, last = parts[0], parts[-1]
    mine = []
    for e in dict.fromkeys(mailtos):
        local = re.sub(r"[^a-z]", "", e.split("@")[0].lower())
        if not is_role_email(e) and ((len(last) > 2 and last in local) or (len(first) > 2 and local.startswith(first))):
            mine.append(e)
    email = mine[0] if len(mine) == 1 else None
    if not (title or email):
        return None
    return Person(name=name, title=title, email=email)


_GENERIC_NAME = re.compile(r"^(home( ?page)?|welcome.*|index|official (web)?site|website|visit.*|learn more.*|"
                           r"about( us)?|contact( us)?|attorneys|services|solutions|careers|blog|news|"
                           r"untitled|page not found|404.*|access denied|just a moment.*|loading.*|"
                           r"re-?captcha|captcha.*|attention required.*|security check.*|one moment.*|\d+)$", re.I)


# "Careers at Tapcheck", "Welcome to Acme": the company is the part after the page words.
_PAGE_WORDS = re.compile(r"(?i)^(?:careers|jobs|work|working|life|open (?:roles|positions))\s+(?:at|with|@)\s+|^welcome to\s+|"
                         r"^join\s+(?:the\s+)?(?=\S)(?=.*\bteam\b)|\s+(?:careers|jobs)$")


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _domain_match(name: str, domain: str) -> int:
    """How much a name looks like the company behind a domain (0 = not at all)."""
    root = _compact(re.sub(r"^www\.", "", domain or "").split(".")[0])
    c = _compact(name)
    if not root or not c:
        return 0
    if c == root:
        return 4
    if root in c or (len(c) >= 3 and c in root):
        return 3
    initials = "".join(w[0] for w in re.findall(r"[A-Za-z0-9]+", name)).lower()
    if len(initials) >= 2 and (root.startswith(initials) or initials.startswith(root)):
        return 2
    return 0


def best_company_name(domain: str, site_name: str | None = None, title: str | None = None,
                      anchor: str | None = None) -> str:
    """Pick a company's name: the site's own name if it gives one, else the part of the
    page title / directory link text that looks like the domain, else a short title part,
    else the domain itself. Generic text ("Home", "Visit Website", a headline) is skipped."""
    cands: list[tuple[int, str]] = []
    if site_name and not _GENERIC_NAME.match(site_name.strip()):
        # Site names sometimes carry a tagline ("Alkami Technology - Digital Banking Solutions"):
        # keep the part that looks like the domain, else the first part.
        parts = [p.strip() for p in re.split(r"\s+[|\-–—:•·]\s+|\s*[|–—•·]\s*", site_name) if p.strip()]
        parts = [_PAGE_WORDS.sub("", p).strip() or p for p in parts]
        best = max(parts, key=lambda p: (_domain_match(p, domain), -parts.index(p))) if parts else site_name
        cands.append((5 + _domain_match(best, domain), best.strip()))
    for src, bonus in ((title, 0), (anchor, 0)):
        for part in re.split(r"\s+[|\-–—:•·]\s+|\s*[|–—•·]\s*", src or ""):
            part = part.strip(" \t\"'")
            part = _PAGE_WORDS.sub("", part).strip() or part
            if 2 <= len(part) <= 60 and not _GENERIC_NAME.match(part):
                score = _domain_match(part, domain)
                cands.append((score * 2 + bonus, part))
    good = [c for c in cands if c[0] >= 4]
    if good:
        return max(good, key=lambda c: (c[0], -len(c[1])))[1][:120]
    short = [p for s, p in cands if len(p.split()) <= 4]
    if short:
        return min(short, key=len)[:120]
    root = re.sub(r"^www\.", "", domain or "").split(".")[0]
    return root.replace("-", " ").title() if root else (domain or "")


def extract_page(html: str, url: str, region: str = "US") -> PageResult:
    soup = BeautifulSoup(html, "html.parser")
    res = PageResult(url=url)
    if soup.title and soup.title.string:
        res.title = soup.title.string.strip()[:200]

    res.people.extend(_jsonld_people(soup))
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if meta and meta.get("content"):
        res.description = meta["content"].strip()[:500]
    for attrs in ({"property": "og:site_name"}, {"name": "application-name"}, {"name": "apple-mobile-web-app-title"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and (tag.get("content") or "").strip():
            res.site_name = tag["content"].strip()[:120]
            break
    if not res.site_name:
        m = re.search(r'"@type"\s*:\s*"(?:Organization|Corporation|LocalBusiness|ProfessionalService)"[^{}]{0,400}?"name"\s*:\s*"([^"]{2,100})"',
                      html)
        if m:
            res.site_name = m.group(1).strip()
    res.ats = find_ats(html)
    res.jobs = _jsonld_jobs(soup, url)
    known_jobs = {j["url"] for j in res.jobs}
    res.jobs += [j for j in _job_links(soup, url) if j["url"] not in known_jobs]

    mailtos: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if low.startswith("mailto:"):
            email = clean_email(href)
            if email:
                res.emails.add(email)
                mailtos.append(email)
                label = a.get_text(" ", strip=True)
                if looks_like_name(label):
                    res.people.append(Person(name=label, email=email))
        elif low.startswith("tel:"):
            phone = normalize_phone(href[4:], region)
            if phone:
                res.phones.add(phone)
        elif low.startswith(("http://", "https://", "/")):
            absolute = urljoin(url, href).split("#")[0]
            if social_kind(absolute):
                res.socials.add(absolute.rstrip("/"))
            else:
                res.links.append(absolute)
                if absolute not in res.link_text:
                    res.link_text[absolute] = a.get_text(" ", strip=True)[:120]

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    me = _profile_person(soup, mailtos)
    if me:
        same = next((p for p in res.people if p.name and p.name.lower() == me.name.lower()), None)
        if same:  # already spotted: add what the bio page knows
            same.title, same.email = same.title or me.title, same.email or me.email
        else:
            res.people.append(me)
    known = {p.name.lower() for p in res.people if p.name}
    for p in _team_members(soup):
        if p.name.lower() not in known:
            res.people.append(p)
    text = soup.get_text(" ", strip=True)
    res.text = text[:20000]
    res.headcount = find_headcount(text, html)
    res.emails |= find_emails(text)
    res.phones |= find_phones(text, region)
    for p in res.people:
        if p.email:
            res.emails.add(p.email)
        if p.phone:
            res.phones.add(p.phone)
    return res


def extract_text(text: str, region: str = "US") -> tuple[set[str], set[str]]:
    """Emails and phones from plain text or an HTML fragment."""
    if "<" in text and ">" in text:
        r = extract_page(text, "", region)
        return r.emails, r.phones
    return find_emails(text), find_phones(text, region)
