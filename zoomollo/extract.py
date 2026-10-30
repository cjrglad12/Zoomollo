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

_NAME_RE = re.compile(r"^[A-Z][a-zA-Z'’-]+(?:\s+[A-Z]\.?)?(?:\s+[A-Z][a-zA-Z'’-]+){1,2}$")


@dataclass
class Person:
    name: str | None = None
    title: str | None = None
    email: str | None = None
    phone: str | None = None


@dataclass
class PageResult:
    url: str
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    people: list[Person] = field(default_factory=list)
    socials: set[str] = field(default_factory=set)
    links: list[str] = field(default_factory=list)
    title: str | None = None


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
    local = email.split("@", 1)[0].lower()
    return re.sub(r"[^a-z]", "", local) in ROLE_LOCALPARTS


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
                if name or email:
                    people.append(Person(name=name, title=title, email=email, phone=phone))
            for v in node.values():
                if isinstance(v, (list, dict)):
                    walk(v)

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(tag.string or ""))
        except (json.JSONDecodeError, TypeError):
            continue
    return people


def extract_page(html: str, url: str, region: str = "US") -> PageResult:
    soup = BeautifulSoup(html, "html.parser")
    res = PageResult(url=url)
    if soup.title and soup.title.string:
        res.title = soup.title.string.strip()[:200]

    res.people.extend(_jsonld_people(soup))

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

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
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
