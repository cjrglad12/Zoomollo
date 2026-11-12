"""Find companies from a plain description: "what" + "where".

No search engines or paid services. Sources:
  * OpenStreetMap (free public map data): businesses tagged in the city's
    bounding box, often with website, phone and address.
  * Business groups: chambers of commerce and business/trade associations in
    the city (also from the map). Their member directories are crawled and the
    member websites they link to become candidates.
  * Links between companies: partner / supplier / client links on company
    websites (handled in pipeline.prospect, checked with check_site()).
  * Public data files you add (license lists, permit records): datasources.py.
  * Website guessing for businesses listed without a website: likely
    addresses are tried and accepted only if the name or phone matches.
"""

from __future__ import annotations

import logging
import re
import socket
import time
from functools import lru_cache
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

from .crawler import USER_AGENT, Crawler, normalize_domain
from .extract import PageResult, social_kind
from .orgmap import domain_of, norm_company
from .exclusions import staffing_signal

log = logging.getLogger(__name__)

NOMINATIM = "https://nominatim.openstreetmap.org/search"
# Public map-data servers, tried in order; each is often overloaded (502/504), so _overpass()
# retries and falls back to the next one instead of giving up on the first error.
OVERPASS_SERVERS = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]

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
    # Chamber website platforms and big portals linked from member directories.
    "growthzone.com", "growthzoneapp.com", "chambermaster.com", "micronetonline.com", "weblinkconnect.com",
    "memberclicks.net", "chamberorganizer.com", "uschamber.com", "yahoo.com", "msn.com",
    "constantcontactpages.com", "constantcontact.com", "mailchimp.com", "list-manage.com", "eventbrite.com",
    "zoom.us", "wixsite.com", "squarespace.com", "wordpress.com", "vimeo.com", "spotify.com", "goo.gl",
    "mozilla.org", "givebutter.com", "clickup.com", "memberzone.com", "paypal.com", "gofundme.com",
    "donorbox.org", "typeform.com", "jotform.com", "hubspot.com", "calendly.com",
}
# Map entries that are public places, not employers to recruit for.
_NOT_EMPLOYER_TAGS = {("office", "government"), ("amenity", "library"), ("amenity", "school"),
                      ("amenity", "college"), ("amenity", "university"), ("amenity", "parcel_locker"),
                      ("leisure", "park"), ("office", "military"), ("military", "office")}
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
    hint: bool = False                      # listing already mentioned the industry
    needs_check: bool = False               # found via a link: check its homepage first
    contact_name: str | None = None         # e.g. owner / qualifier from a license record
    contact_title: str | None = None

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


def _overpass(query: str, rounds: int = 3):
    """Run a map-data query, trying each server in turn for a few rounds."""
    last: Exception | None = None
    for attempt in range(rounds):
        for url in OVERPASS_SERVERS:
            try:
                return _http_post(url, data={"data": query})
            except requests.RequestException as e:
                last = e
                log.warning("Map server %s failed (%s); trying the next one", url, e)
        time.sleep(10 * (attempt + 1))
    raise last


def is_company_domain(domain: str) -> bool:
    return bool(domain) and not any(domain == d or domain.endswith("." + d) for d in NOT_COMPANIES) \
        and not domain.endswith((".gov", ".edu", ".mil"))


@lru_cache(maxsize=256)
def geocode(where: str) -> tuple[float, float, float, float] | None:
    """(south, west, north, east) bounding box for a US place."""
    data = _http_get(NOMINATIM, params={"q": where, "format": "json", "limit": 1, "countrycodes": "us"})
    if not data:
        return None
    s, n, w, e = (float(x) for x in data[0]["boundingbox"])
    time.sleep(1)  # Nominatim usage policy: max 1 request/second
    return s, w, n, e


_FILLER = {"and", "the", "companies", "company", "firms", "firm", "services", "businesses", "business",
           "near", "local"}
# Describe a market rather than the work; only used if nothing more specific is given.
_MODIFIERS = {"commercial", "residential", "industrial", "professional", "small", "large", "best", "top",
              "new", "independent", "family", "owned", "licensed", "general", "full", "service"}


def keyword_regex(what: str) -> str:
    low = what.lower()
    words = [w for w in re.findall(r"[a-z0-9]+", low) if len(w) > 2 and w not in _FILLER]
    terms = {w for w in words if w not in _MODIFIERS} or set(words)
    for k, vals in SYNONYMS.items():
        if k in low + " ":
            terms.update(vals)
    # "air_conditioning" (map-data spelling) should also match "air conditioning" / "air-conditioning".
    return "|".join(sorted(re.escape(t).replace("_", "[ _-]") for t in terms))


# Specialties of a trade: a map listing or company name rarely spells these out
# ("SPIARS Engineering" for "mechanical engineering"), so lenient checks skip them.
_SPECIALTY = {"mep", "mechanical", "electrical", "civil", "structural", "plumbing", "environmental"}
# Public bodies and places that show up in map searches but aren't employers to recruit for.
_PUBLIC_BODY = re.compile(r"\b(town of|city of|county of|library|university|college|army|corps of engineers|"
                          r"extension service|project office|entrance processing|public works)\b", re.I)


def matches_all(what: str, text: str, lenient: bool = False) -> bool:
    """Stricter than keyword_regex: every main word of the search must appear (allowing
    word endings, e.g. engineer/engineering, and the word's synonyms), so "data analytics"
    no longer matches any site that merely says "your data". lenient=True skips trade
    specialties (MEP, mechanical...) when another word is left to check."""
    low = what.lower()
    words = [w for w in re.findall(r"[a-z0-9]+", low) if len(w) > 2 and w not in _FILLER]
    words = [w for w in words if w not in _MODIFIERS] or words
    if lenient:
        words = [w for w in words if w not in _SPECIALTY] or words
    for w in words:
        stem = w[:max(4, len(w) - 3)] if len(w) > 4 else w
        alts = [rf"\b{re.escape(stem)}\w*"]
        for k, vals in SYNONYMS.items():
            if k.strip() and k.strip() in w:
                alts += [rf"\b{re.escape(v).replace('_', '[ _-]')}" for v in vals if v not in ("it", "company")]
        if not re.search("|".join(alts), text or "", re.I):
            return False
    return True


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
    data = _overpass(query)

    out: dict[str, Candidate] = {}
    for el in data.get("elements", []):
        t = el.get("tags", {})
        name = t.get("name")
        if not name or t.get("office") == "employment_agency" or staffing_signal(name, t.get("description")):
            continue
        if any(t.get(k) == v for k, v in _NOT_EMPLOYER_TAGS) or _PUBLIC_BODY.search(name):
            continue
        # The map query matches any one word; note whether every word of the search shows up
        # in the name or the map's own description of the place (checked in find_companies).
        tag_text = " ".join([name] + [str(t.get(k, "")).replace("_", " ") for k in
                                      ("office", "craft", "shop", "industrial", "healthcare", "description")])
        site = t.get("website") or t.get("contact:website") or t.get("url")
        domain = normalize_domain(site) if site else None
        if domain and not is_company_domain(domain):
            domain = None
        addr = " ".join(x for x in [t.get("addr:housenumber"), t.get("addr:street"),
                                    t.get("addr:city"), t.get("addr:state"), t.get("addr:postcode")] if x)
        c = Candidate(name=name, domain=domain, phone=t.get("phone") or t.get("contact:phone"),
                      email=t.get("email") or t.get("contact:email"), address=addr or None,
                      sources={"openstreetmap"}, hint=matches_all(what, tag_text, lenient=True))
        out.setdefault(c.key, c)
    return list(out.values())[:limit]


def _name_from_title(title: str) -> str:
    parts = [p.strip() for p in re.split(r"\s+[|\-–—:]\s+", title) if p.strip()]
    parts = [p for p in parts if p.lower() not in {"home", "homepage", "welcome"}] or parts or [title]
    return min(parts, key=len)[:120]


STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia",
}


@lru_cache(maxsize=4096)
def split_where(where: str) -> tuple[str, str | None]:
    """'Austin, TX' -> ('Austin', 'TX'); 'Austin Texas' -> ('Austin', 'TX')."""
    parts = [p.strip() for p in re.split(r",", where) if p.strip()]
    city, state = (parts[0] if parts else where.strip()), None
    tail = parts[-1] if len(parts) > 1 else ""
    for abbr, name in STATES.items():
        if tail.upper() == abbr or tail.lower() == name.lower():
            state = abbr
        elif not tail and city.lower().endswith(" " + name.lower()):
            city, state = city[: -len(name)].strip(), abbr
    return city, state


def location_hit(text: str, where: str) -> bool:
    city, state = split_where(where)
    t = text or ""
    if city and re.search(rf"\b{re.escape(city)}\b", t, re.I):
        return True
    if state and (re.search(rf"\b{state}\b", t) or re.search(rf"\b{STATES[state]}\b", t, re.I)):
        return True
    return False


def page_text(page: PageResult) -> str:
    return " ".join(x for x in [page.title, page.description, page.text] if x)


# --- checking a website -------------------------------------------------------

def _resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443)
        return True
    except (socket.gaierror, UnicodeError, OSError):
        return False


def check_site(crawler: Crawler, domain: str, what: str, where: str,
               trusted_location: bool = False) -> tuple[bool, str | None]:
    """Fetch a homepage and decide if it's a relevant local business.
    Returns (ok, company name from the page title)."""
    from .exclusions import staffing_signal
    from .extract import extract_page
    got = crawler.fetch(f"https://{domain}")
    if not got:
        return False, None
    page = extract_page(got[1], got[0])
    text = page_text(page)
    if staffing_signal(page.title, page.description, domain=domain):
        return False, None
    kw = matches_all(what, text)
    ok = kw and (trusted_location or location_hit(text, where))
    from .extract import best_company_name
    return ok, (best_company_name(domain, page.site_name, page.title) if (page.title or page.site_name) else None)


def guess_website(crawler: Crawler, name: str, where: str, phone: str | None = None,
                  taken: set[str] = frozenset()) -> str | None:
    """Try likely domains for a business listed without a website. Accept one
    only if its homepage shows the business's phone number, or its name plus
    the city/state."""
    from .extract import extract_page
    core = norm_company(name)
    words = core.split()
    if not words:
        return None
    stems = list(dict.fromkeys([
        "".join(words), "-".join(words),
        "".join(w for w in words if w not in {"services", "service", "solutions", "and"}),
    ]))
    digits = re.sub(r"\D", "", phone or "")[-10:]
    for stem in stems:
        for tld in (".com", ".net", ".us", ".co"):
            domain = stem + tld
            if len(stem) < 4 or domain in taken or not _resolves(domain):
                continue
            got = crawler.fetch(f"https://{domain}")
            if not got:
                continue
            page = extract_page(got[1], got[0])
            text = page_text(page)
            if digits and digits in re.sub(r"\D", "", text):
                return domain
            if norm_company(name) in norm_company(text) and location_hit(text, where):
                return domain
    return None


# --- business groups (chambers of commerce, associations) ---------------------

_HUB_NAME = (r"chamber of commerce|chamber|business association|business alliance|business council|"
             r"builders association|contractors association|merchants association|association of|"
             r"economic development|business league|board of realtors|bar association|medical society")
_DIRECTORY = re.compile(r"(member|directory|business|list|categor|search|find|ql|browse|industr)", re.I)
_GENERIC_ANCHOR = re.compile(r"^(website|visit( (our )?(web)?site)?|web ?site|www\..*|https?://.*|learn more.*|"
                             r"more info|view (profile|details|website)|details|click here|homepage|link|"
                             r"download|donate|contact( us)?|sign ?up.*|subscribe.*|register.*|log ?in.*|"
                             r"no login.*|apply.*|read more.*)$", re.I)
_NOT_DIRECTORY = re.compile(r"/(events?|news|login|logout|jobs|calendar|hotdeals|marketspace|marketplace|"
                            r"member/newmemberapp|members?/(login|account|portal)|eventregistration)", re.I)


def osm_hubs(where: str, limit: int = 15) -> list[tuple[str, str]]:
    """(name, domain) of chambers of commerce and business associations in the area."""
    bbox = geocode(where)
    if not bbox:
        return []
    b = ",".join(str(x) for x in bbox)
    query = (f'[out:json][timeout:60];('
             f'nwr["office"~"association|chamber|ngo|foundation",i]["name"~"{_HUB_NAME}",i]({b});'
             f'nwr["name"~"{_HUB_NAME}",i]["website"]({b});'
             f'nwr["name"~"{_HUB_NAME}",i]["contact:website"]({b});'
             f');out center tags 200;')
    data = _overpass(query)
    out: dict[str, str] = {}
    for el in data.get("elements", []):
        t = el.get("tags", {})
        site = t.get("website") or t.get("contact:website") or t.get("url")
        if t.get("name") and site:
            d = normalize_domain(site)
            if d and is_company_domain(d):
                out.setdefault(d, t["name"])
    # Chambers first: their member directories are the richest.
    ranked = sorted(out.items(), key=lambda kv: "chamber" not in kv[1].lower())
    return [(name, d) for d, name in ranked][:limit]


def hub_members(crawler: Crawler, hub_domain: str, what: str, max_pages: int = 40,
                start_paths: list[str] | None = None) -> list[Candidate]:
    """Crawl a business group's site (member directory first) and collect the
    member websites it links to. Members whose listing mentions the industry
    are marked with 'hint' so they're checked first."""
    rx = keyword_regex(what)
    kw = re.compile(rf"\b({rx})", re.I) if rx else None

    # A specific directory page given by the user (e.g. /list or /sponsors-partners): stay in
    # that section of the site and only take companies from pages inside it, so links from
    # the group's events, news and login pages don't come along.
    sections = [("/" + p.strip("/").split("/")[0]) for p in (start_paths or []) if p.strip("/")]

    def in_section(url: str) -> bool:
        path = urlparse(url).path or "/"
        return not sections or any(path == s or path.startswith(s + "/") for s in sections)

    def scorer(url: str, text: str) -> int:
        s = 6 if _DIRECTORY.search(url + " " + text) else 0
        if kw and kw.search(url.replace("-", " ") + " " + text):
            s += 12
        if sections:
            s += 40 if in_section(url) else 0
            s -= 20 if _NOT_DIRECTORY.search(urlparse(url).path) else 0
        return s

    seeds = ["/", "/member-directory", "/directory", "/members", "/business-directory", "/list",
             "/members/directory", "/find-a-member", "/member-search"]
    if start_paths:  # a specific directory / sponsor page given by the user goes first
        seeds = list(dict.fromkeys([p for p in start_paths if p] + ([] if sections else seeds)))
    crawler.max_pages = max_pages
    out: dict[str, Candidate] = {}
    for page in crawler.crawl(hub_domain, seed_paths=seeds, scorer=scorer, include_subdomains=True,
                              keep_query=True, use_sitemap=False):
        if not in_section(page.url):
            continue
        context = " ".join(x for x in [page.title, page.url] if x)
        page_hint = bool(kw and kw.search(context.replace("-", " ")))
        for link in page.links:
            d = domain_of(link)
            if not d or d == hub_domain or d.endswith("." + hub_domain) or hub_domain.endswith("." + d):
                continue
            if ".".join(d.split(".")[-2:]) == ".".join(hub_domain.split(".")[-2:]):
                continue  # another part of the group's own site (careers.x.org vs business.x.org)
            if social_kind(link) or not is_company_domain(d):
                continue
            text = page.link_text.get(link, "").strip()
            name = text if text and not _GENERIC_ANCHOR.match(text) and len(text) <= 80 else None
            hint = page_hint or bool(kw and name and kw.search(name))
            c = out.get(d)
            if not c:
                c = out[d] = Candidate(name=name or d, domain=d, sources={f"member of {hub_domain}"})
            elif name and c.name == d:
                c.name = name
            c.hint = c.hint or hint
    return sorted(out.values(), key=lambda c: not c.hint)


# --- putting it together ------------------------------------------------------

def find_companies(what: str, where: str, limit: int = 25, use_osm: bool = True, use_hubs: bool = True,
                   crawler: Crawler | None = None, progress=None) -> list[Candidate]:
    """Candidates from map data and business-group directories. Directory members
    are only kept after their own homepage checks out (industry + location)."""
    say = progress or (lambda m: None)
    crawler = crawler or Crawler(max_pages=40, delay=1.0)
    merged: dict[str, Candidate] = {}

    if use_osm:
        try:
            osm = osm_companies(what, where, limit * 3)
            # Keep a map entry only if every word of the search matches its name/description,
            # or else its own homepage (e.g. "SPIARS Engineering" for "mechanical engineering").
            kept, site_checks = 0, 0
            for c in osm:
                if not c.hint:
                    if not c.domain or site_checks >= limit * 3:
                        continue
                    site_checks += 1
                    if not check_site(crawler, c.domain, what, where, trusted_location=True)[0]:
                        continue
                merged.setdefault(c.key, c)
                kept += 1
            say(f"Map data: {len(osm)} possible businesses, {kept} match every word of the search")
        except requests.RequestException as e:
            say(f"Map data unavailable: {e}")

    if use_hubs and len([c for c in merged.values() if c.domain]) < limit:
        try:
            hubs = osm_hubs(where)
        except requests.RequestException as e:
            hubs = []
            say(f"Business groups unavailable: {e}")
        say(f"Business groups to check: {len(hubs)}")
        checks = 0
        for hub_name, hub_domain in hubs:
            members = hub_members(crawler, hub_domain, what)
            kept = 0
            for c in members:
                if c.key in merged or checks >= limit * 6:
                    continue
                checks += 1
                ok, page_name = check_site(crawler, c.domain, what, where, trusted_location=True)
                if ok:
                    if page_name and (c.name == c.domain):
                        c.name = page_name
                    merged[c.key] = c
                    kept += 1
            say(f"{hub_name}: {len(members)} member sites, {kept} match")
            if len([c for c in merged.values() if c.domain]) >= limit:
                break

    # Websites first: those are the ones we can map.
    ranked = sorted(merged.values(), key=lambda c: (c.domain is None, -len(c.sources)))
    picked = ranked[:limit]
    taken = {c.domain for c in merged.values() if c.domain}
    for c in picked:
        if not c.domain:
            c.domain = guess_website(crawler, c.name, where, c.phone, taken)
            if c.domain:
                taken.add(c.domain)
                c.sources.add("website guessed")
    return picked
