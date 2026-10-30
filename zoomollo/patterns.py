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
]


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


def guess_emails(full_name: str, domain: str, known_pattern: str | None = None) -> list[str]:
    split = split_name(full_name)
    if not split:
        return []
    order = ([known_pattern] if known_pattern else []) + [p for p in PATTERNS if p != known_pattern]
    seen, out = set(), []
    for p in order:
        e = f"{render(p, *split)}@{domain.lower()}"
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


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
