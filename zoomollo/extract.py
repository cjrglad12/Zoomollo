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


def find_ats(text: str) -> set[tuple[str, str]]:
    out = set()
    for board, rx in ATS_RE:
        for m in rx.finditer(text or ""):
            slug = m.group(1).strip("/").lower()
            if slug and slug not in _ATS_NOT_SLUGS:
                out.add((board, slug))
    return out


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


def looks_like_name(text: str | None) -> bool:
    return bool(text) and bool(_NAME_RE.match(text.strip())) and "@" not in text


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
        if href not in seen:
            seen.add(href)
            jobs.append({"title": text, "url": href, "external_id": href, "location": None,
                         "posted_at": None, "description": ""})
    return jobs


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


def extract_page(html: str, url: str, region: str = "US") -> PageResult:
    soup = BeautifulSoup(html, "html.parser")
    res = PageResult(url=url)
    if soup.title and soup.title.string:
        res.title = soup.title.string.strip()[:200]

    res.people.extend(_jsonld_people(soup))
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if meta and meta.get("content"):
        res.description = meta["content"].strip()[:500]
    res.ats = find_ats(html)
    res.jobs = _jsonld_jobs(soup, url)
    known_jobs = {j["url"] for j in res.jobs}
    res.jobs += [j for j in _job_links(soup, url) if j["url"] not in known_jobs]

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if low.startswith("mailto:"):
            email = clean_email(href)
            if email:
                res.emails.add(email)
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
    known = {p.name.lower() for p in res.people if p.name}
    for p in _team_members(soup):
        if p.name.lower() not in known:
            res.people.append(p)
    text = soup.get_text(" ", strip=True)
    res.text = text[:20000]
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
