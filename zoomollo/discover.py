"""Find companies from a plain description: "what" + "where".

Sources:
  * OpenStreetMap (free, no key): businesses tagged in the city's bounding box,
    often with website, phone and address.
  * A web search provider, if configured (see search.py): "<what> companies in
    <where>" style queries, plus job-board searches for companies hiring there.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

import requests

from .crawler import USER_AGENT, normalize_domain
from .extract import ATS_RE, find_ats
from .orgmap import domain_of, mentions, norm_company
from .search import SearchProvider

log = logging.getLogger(__name__)

NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS = "https://overpass-api.de/api/interpreter"

# Plain-English industry words -> OpenStreetMap tag values worth matching too.
SYNONYMS = {
    "law": ["lawyer", "legal", "attorney"], "legal": ["lawyer"], "attorney": ["lawyer"],
    "accounting": ["accountant", "tax_advisor"], "cpa": ["accountant"], "tax": ["tax_advisor"],
    "hvac": ["hvac", "heating", "air_conditioning"], "plumb": ["plumber"], "electric": ["electrician"],
    "roof": ["roofer"], "construction": ["construction_company", "builder", "contractor"],
    "contractor": ["contractor", "builder"], "builder": ["builder"],
    "real estate": ["estate_agent", "property_management"], "property": ["property_management"],
    "insurance": ["insurance"], "financial": ["financial", "financial_advisor"],
    "software": ["it", "software", "company"], "tech": ["it", "software"], "it ": ["it"],
    "marketing": ["advertising_agency", "marketing"], "advertising": ["advertising_agency"],
    "staffing": ["employment_agency"], "recruit": ["employment_agency"],
    "architect": ["architect"], "engineering": ["engineer"], "dental": ["dentist"],
    "dentist": ["dentist"], "medical": ["doctors", "clinic"], "clinic": ["clinic"],
    "veterinar": ["veterinary"], "logistics": ["logistics"], "moving": ["moving_company"],
    "landscap": ["landscaper", "gardener"], "clean": ["cleaning"], "consult": ["consulting"],
    "manufactur": ["manufacture", "factory"], "brewer": ["brewery"], "printing": ["printer"],
}

# Results from these sites are directories/aggregators, not company websites.
NOT_COMPANIES = {
    "yelp.com", "linkedin.com", "indeed.com", "glassdoor.com", "facebook.com", "instagram.com",
    "twitter.com", "x.com", "youtube.com", "wikipedia.org", "bbb.org", "yellowpages.com",
    "clutch.co", "thumbtack.com", "angi.com", "angieslist.com", "homeadvisor.com", "mapquest.com",
    "google.com", "bing.com", "tripadvisor.com", "zoominfo.com", "apollo.io", "crunchbase.com",
    "manta.com", "chamberofcommerce.com", "buildzoom.com", "houzz.com", "porch.com", "nextdoor.com",
    "ziprecruiter.com", "simplyhired.com", "monster.com", "careerbuilder.com", "builtin.com",
    "builtinaustin.com", "upcity.com", "expertise.com", "reddit.com", "quora.com", "medium.com",
    "dnb.com", "bizapedia.com", "opencorporates.com", "superpages.com", "angi.com", "birdeye.com",
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "tiktok.com", "pinterest.com",
    "apple.com", "amazon.com", "salary.com", "comparably.com", "rocketreach.co", "signalhire.com",
    "lusha.com", "owler.com", "cbinsights.com", "pitchbook.com", "techbehemoths.com", "designrush.com",
    "goodfirms.co", "sortlist.com", "themanifest.com", "trustpilot.com", "angel.co", "wellfound.com",
}
_LISTICLE = re.compile(r"^(the\s+)?(top|best|\d+\s+(best|top))\b|\b(near me|list of|directory)\b", re.I)


@dataclass
class Candidate:
    name: str
    domain: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    sources: set[str] = field(default_factory=set)
    ats: set[tuple[str, str]] = field(default_factory=set)

    @property
    def key(self) -> str:
        return self.domain or "name:" + re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")


def _http_get(url, **kw):
    kw.setdefault("timeout", 30)
    kw.setdefault("headers", {"User-Agent": USER_AGENT})
    r = requests.get(url, **kw)
    r.raise_for_status()
    return r.json()


def _http_post(url, **kw):
    kw.setdefault("timeout", 120)
    kw.setdefault("headers", {"User-Agent": USER_AGENT})
    r = requests.post(url, **kw)
    r.raise_for_status()
    return r.json()


def is_company_domain(domain: str) -> bool:
    return bool(domain) and not any(domain == d or domain.endswith("." + d) for d in NOT_COMPANIES) \
        and not domain.endswith((".gov", ".edu", ".mil"))


def geocode(where: str) -> tuple[float, float, float, float] | None:
    """(south, west, north, east) bounding box for a US place."""
    data = _http_get(NOMINATIM, params={"q": where, "format": "json", "limit": 1, "countrycodes": "us"})
    if not data:
        return None
    s, n, w, e = (float(x) for x in data[0]["boundingbox"])
    time.sleep(1)  # Nominatim usage policy: max 1 request/second
    return s, w, n, e


def keyword_regex(what: str) -> str:
    low = what.lower()
    terms = {w for w in re.findall(r"[a-z0-9]+", low) if len(w) > 2
             and w not in {"and", "the", "companies", "company", "firms", "firm", "services", "businesses"}}
    for k, vals in SYNONYMS.items():
        if k in low + " ":
            terms.update(vals)
    return "|".join(sorted(re.escape(t) for t in terms))


def osm_companies(what: str, where: str, limit: int = 200) -> list[Candidate]:
    bbox = geocode(where)
    if not bbox:
        log.warning("Could not find '%s' on the map", where)
        return []
    rx = keyword_regex(what)
    if not rx:
        return []
    b = ",".join(str(x) for x in bbox)
    sel = []
    for k in ("office", "craft", "shop", "healthcare", "amenity", "industrial"):
        sel.append(f'nwr["{k}"~"{rx}",i]({b});')
        sel.append(f'nwr["name"~"{rx}",i]["{k}"]({b});')
    sel.append(f'nwr["description"~"{rx}",i]["name"]({b});')
    query = f'[out:json][timeout:90];({"".join(sel)});out center tags {limit * 2};'
    data = _http_post(OVERPASS, data={"data": query})

    out: dict[str, Candidate] = {}
    for el in data.get("elements", []):
        t = el.get("tags", {})
        name = t.get("name")
        if not name:
            continue
        site = t.get("website") or t.get("contact:website") or t.get("url")
        domain = normalize_domain(site) if site else None
        if domain and not is_company_domain(domain):
            domain = None
        addr = " ".join(x for x in [t.get("addr:housenumber"), t.get("addr:street"),
                                    t.get("addr:city"), t.get("addr:state"), t.get("addr:postcode")] if x)
        c = Candidate(name=name, domain=domain, phone=t.get("phone") or t.get("contact:phone"),
                      email=t.get("email") or t.get("contact:email"), address=addr or None,
                      sources={"openstreetmap"})
        out.setdefault(c.key, c)
    return list(out.values())[:limit]


def _name_from_title(title: str) -> str:
    parts = [p.strip() for p in re.split(r"\s+[|\-–—:]\s+", title) if p.strip()]
    parts = [p for p in parts if p.lower() not in {"home", "homepage", "welcome"}] or parts or [title]
    return min(parts, key=len)[:120]


def search_companies(provider: SearchProvider, what: str, where: str, pages: int = 1) -> list[Candidate]:
    city = where.split(",")[0].strip()
    queries = [
        f"{what} companies in {where}",
        f"{what} {where}",
        f"{what} firm {city} about us",
        f'(site:boards.greenhouse.io OR site:jobs.lever.co OR site:jobs.ashbyhq.com OR '
        f'site:apply.workable.com) "{what}" "{city}"',
    ][: 2 + 2 * pages]
    out: dict[str, Candidate] = {}
    for q in queries:
        for r in provider.search(q):
            ats = find_ats(r.url)
            if ats:
                board, slug = next(iter(ats))
                c = out.setdefault(f"{board}:{slug}", Candidate(name=slug, sources={provider.name}))
                c.ats |= ats
                continue
            domain = domain_of(r.url)
            if not is_company_domain(domain) or _LISTICLE.search(r.title):
                continue
            c = out.setdefault(domain, Candidate(name=_name_from_title(r.title), domain=domain))
            c.sources.add(provider.name)
    return list(out.values())


def find_website(provider: SearchProvider, name: str, where: str,
                 taken: set[str] = frozenset()) -> str | None:
    """The company's own site: a non-directory result whose title or domain names it."""
    core = norm_company(name).replace(" ", "")
    for r in provider.search(f'"{name}" {where}', count=10):
        d = domain_of(r.url)
        if not is_company_domain(d) or d in taken:
            continue
        if mentions(name, r.title) or (len(core) >= 4 and core in d.replace("-", "")):
            return d
    return None


def find_companies(what: str, where: str, limit: int = 25, provider: SearchProvider | None = None,
                   use_osm: bool = True, progress=None) -> list[Candidate]:
    say = progress or (lambda m: None)
    merged: dict[str, Candidate] = {}

    def add(c: Candidate):
        cur = merged.get(c.key)
        if not cur:
            merged[c.key] = c
            return
        cur.sources |= c.sources
        cur.ats |= c.ats
        for f in ("phone", "email", "address"):
            if not getattr(cur, f):
                setattr(cur, f, getattr(c, f))

    if use_osm:
        try:
            osm = osm_companies(what, where, limit * 3)
            say(f"OpenStreetMap: {len(osm)} businesses")
            for c in osm:
                add(c)
        except requests.RequestException as e:
            say(f"OpenStreetMap unavailable: {e}")
    if provider:
        web = search_companies(provider, what, where)
        say(f"Web search ({provider.name}): {len(web)} companies")
        for c in web:
            add(c)

    # Websites first: those are the ones we can map.
    ranked = sorted(merged.values(), key=lambda c: (c.domain is None, -len(c.sources)))
    picked = ranked[:limit]
    if provider:
        taken = {c.domain for c in merged.values() if c.domain}
        for c in picked:
            if not c.domain and not c.ats:
                c.domain = find_website(provider, c.name, where, taken)
                if c.domain:
                    taken.add(c.domain)
    return picked


__all__ = ["Candidate", "find_companies", "osm_companies", "search_companies", "ATS_RE"]
