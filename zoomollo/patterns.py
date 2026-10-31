"""Work out a company's email format from known examples and guess addresses."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from functools import lru_cache

# Ordered roughly by how common they are across companies.
PATTERNS = [
    "{first}.{last}",
    "{first}",
    "{f}{last}",
    "{first}{last}",
    "{first}_{last}",
    "{first}{l}",
    "{f}.{last}",
    "{last}.{first}",
    "{last}{f}",
    "{last}",
    "{first}-{last}",
    "{first}.{l}",
    "{f}_{last}",
    "{last}{first}",
    "{last}_{first}",
    "{f}{l}",
    "{last}.{f}",
]

# Common nickname pairs; tried after the formal-name guesses.
NICKNAMES = {
    "robert": ["bob", "rob"], "william": ["bill", "will"], "richard": ["rick", "rich"],
    "james": ["jim", "jimmy"], "john": ["jack"], "joseph": ["joe"], "michael": ["mike"],
    "thomas": ["tom"], "christopher": ["chris"], "daniel": ["dan"], "matthew": ["matt"],
    "anthony": ["tony"], "steven": ["steve"], "stephen": ["steve"], "andrew": ["andy", "drew"],
    "joshua": ["josh"], "kenneth": ["ken"], "edward": ["ed", "ted"], "benjamin": ["ben"],
    "samuel": ["sam"], "nicholas": ["nick"], "jonathan": ["jon"], "timothy": ["tim"],
    "patrick": ["pat"], "gregory": ["greg"], "jeffrey": ["jeff"], "charles": ["charlie", "chuck"],
    "david": ["dave"], "donald": ["don"], "ronald": ["ron"], "douglas": ["doug"],
    "elizabeth": ["liz", "beth"], "jennifer": ["jen", "jenny"], "katherine": ["kate", "katie"],
    "catherine": ["cathy", "kate"], "kimberly": ["kim"], "rebecca": ["becky"], "patricia": ["pat", "trish"],
    "susan": ["sue"], "margaret": ["maggie", "meg"], "deborah": ["deb"], "jessica": ["jess"],
    "christina": ["chris", "tina"], "alexandra": ["alex"], "alexander": ["alex"], "victoria": ["vicky"],
    "bob": ["robert"], "bill": ["william"], "mike": ["michael"], "jim": ["james"], "tom": ["thomas"],
    "chris": ["christopher"], "dan": ["daniel"], "matt": ["matthew"], "steve": ["steven"],
    "joe": ["joseph"], "dave": ["david"], "andy": ["andrew"], "liz": ["elizabeth"], "kate": ["katherine"],
}


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s.lower())


def split_name(full_name: str) -> tuple[str, str] | None:
    parts = [p for p in re.split(r"\s+", full_name.strip()) if p and not p.endswith(".")]
    parts = [p for p in parts if _slug(p)]
    if len(parts) < 2:
        return None
    return _slug(parts[0]), _slug(parts[-1])


def render(pattern: str, first: str, last: str) -> str:
    return pattern.format(first=first, last=last, f=first[:1], l=last[:1])


def infer_pattern(examples: list[tuple[str, str]]) -> tuple[str, float] | None:
    """examples: (full_name, email). Returns (pattern, share_of_examples_matching)."""
    hits: Counter[str] = Counter()
    usable = 0
    for name, email in examples:
        split = split_name(name)
        if not split:
            continue
        local = email.split("@", 1)[0].lower()
        usable += 1
        for p in PATTERNS:
            if render(p, *split) == local:
                hits[p] += 1
                break
    if not hits:
        return None
    pattern, n = hits.most_common(1)[0]
    return pattern, n / usable


def candidates(full_name: str, domain: str, preferred: list[str] | None = None,
               avoid: set[str] = frozenset()) -> list[tuple[str, str]]:
    """Ordered (email, pattern) guesses. `preferred` patterns go first, `avoid`
    patterns (ones that bounced at this company) go last. Nickname variants
    are appended after the formal-name guesses."""
    split = split_name(full_name)
    if not split:
        return []
    first, last = split
    order = list(dict.fromkeys([*(preferred or []), *PATTERNS]))
    order = [p for p in order if p not in avoid] + [p for p in order if p in avoid]
    seen: set[str] = set()
    out: list[tuple[str, str]] = []

    def add(f: str, p: str, tag: str = ""):
        e = f"{render(p, f, last)}@{domain.lower()}"
        if e not in seen:
            seen.add(e)
            out.append((e, p + tag))

    for p in order:
        add(first, p)
    for nick in NICKNAMES.get(first, []):
        for p in order[:4]:
            add(nick, p, " (nickname)")
    return out


def guess_emails(full_name: str, domain: str, known_pattern: str | None = None) -> list[str]:
    return [e for e, _ in candidates(full_name, domain, [known_pattern] if known_pattern else None)]


@lru_cache(maxsize=2048)
def has_mx(domain: str) -> bool | None:
    """True/False if DNS answers, None if the lookup itself failed."""
    try:
        import dns.resolver
        import dns.exception

        try:
            return len(dns.resolver.resolve(domain, "MX", lifetime=5)) > 0
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return False
        except dns.exception.DNSException:
            return None
    except ImportError:
        return None
