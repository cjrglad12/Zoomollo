"""Find license and permit datasets on government open-data portals automatically.

Many states and cities publish contractor / professional license lists and
building permits on open-data portals that share one platform (Socrata).
Each portal can search its own catalog, so for a prospect run Zoomollo:

  1. picks the portals for the city and its state (a built-in list, plus a few
     common web-address patterns checked on your computer),
  2. asks each portal's catalog for datasets about licenses / permits /
     contractors / business registrations that match the industry,
  3. keeps datasets that have a business-name column, and reads matching rows
     through datasources.py (businesses on many records are listed first).

Everything comes straight from the government portals; there's no account,
key or middleman. Portals on other platforms aren't searched automatically;
add their CSV/JSON links as data sources by hand.
"""

from __future__ import annotations

import logging
import re

import requests

from .crawler import USER_AGENT
from .datasources import _match_columns
from .discover import STATES, Candidate, keyword_regex, split_where

log = logging.getLogger(__name__)

# State-wide portals (licenses, business registrations).
STATE_PORTALS = {
    "TX": ["data.texas.gov"], "WA": ["data.wa.gov"], "CO": ["data.colorado.gov"], "NY": ["data.ny.gov"],
    "CT": ["data.ct.gov"], "IL": ["data.illinois.gov"], "MD": ["opendata.maryland.gov"],
    "OR": ["data.oregon.gov"], "DE": ["data.delaware.gov"], "VT": ["data.vermont.gov"],
    "IA": ["mydata.iowa.gov", "data.iowa.gov"], "PA": ["data.pa.gov"], "NJ": ["data.nj.gov"],
    "MO": ["data.mo.gov"], "UT": ["opendata.utah.gov"], "MI": ["data.michigan.gov"],
    "CA": ["data.ca.gov"], "HI": ["data.hawaii.gov"], "VA": ["data.virginia.gov"],
}

# City / county portals (building permits, business licenses).
CITY_PORTALS = {
    ("austin", "TX"): ["data.austintexas.gov"], ("dallas", "TX"): ["www.dallasopendata.com"],
    ("chicago", "IL"): ["data.cityofchicago.org"], ("new york", "NY"): ["data.cityofnewyork.us"],
    ("san francisco", "CA"): ["data.sfgov.org"], ("seattle", "WA"): ["data.seattle.gov"],
    ("los angeles", "CA"): ["data.lacity.org", "data.lacounty.gov"], ("new orleans", "LA"): ["data.nola.gov"],
    ("kansas city", "MO"): ["data.kcmo.org"], ("cincinnati", "OH"): ["data.cincinnati-oh.gov"],
    ("baton rouge", "LA"): ["data.brla.gov"], ("cambridge", "MA"): ["data.cambridgema.gov"],
    ("somerville", "MA"): ["data.somervillema.gov"], ("providence", "RI"): ["data.providenceri.gov"],
    ("oakland", "CA"): ["data.oaklandca.gov"], ("fort worth", "TX"): ["data.fortworthtexas.gov"],
    ("king county", "WA"): ["data.kingcounty.gov"],
    ("montgomery county", "MD"): ["data.montgomerycountymd.gov"],
    ("gainesville", "FL"): ["data.cityofgainesville.org"], ("norfolk", "VA"): ["data.norfolk.gov"],
    ("little rock", "AR"): ["data.littlerock.gov"], ("mesa", "AZ"): ["data.mesaaz.gov"],
    ("chattanooga", "TN"): ["www.chattadata.org"], ("buffalo", "NY"): ["data.buffalony.gov"],
    ("roseville", "CA"): ["data.roseville.ca.us"], ("everett", "WA"): ["data.everettwa.gov"],
}

# What to ask each catalog for, besides the industry words themselves.
CATALOG_QUERIES = ["{kw} license", "{kw} contractor", "{kw} permit", "building permits", "contractor license",
                   "business license", "professional license"]
_GOOD_DATASET = re.compile(r"licen[cs]|permit|contractor|registration|registrant|business|vendor", re.I)
_BAD_DATASET = re.compile(r"violation|complaint|inspection result|crime|arrest|salary|payroll|budget|"
                          r"311|citation|lawsuit|claim", re.I)


def _get(url: str, **params):
    r = requests.get(url, params=params, timeout=30, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def guessed_portals(city: str, state: str | None) -> list[str]:
    """Common open-data web addresses for a city (checked before use)."""
    c = re.sub(r"[^a-z]", "", city.lower())
    if not c:
        return []
    st = (state or "").lower()
    full = STATES.get(state or "", "").lower().replace(" ", "")
    out = [f"data.{c}{full}.gov", f"data.{c}{st}.gov", f"data.cityof{c}.org", f"data.{c}.gov",
           f"data.{c}{st}.us", f"opendata.{c}.gov"]
    return [d for d in dict.fromkeys(out) if d.count(".") >= 2 and ".." not in d]


def portals_for(where: str, guess: bool = True) -> list[str]:
    city, state = split_where(where)
    out = list(CITY_PORTALS.get((city.lower(), state), []))
    if guess:
        out += guessed_portals(city, state)
    out += STATE_PORTALS.get(state or "", [])
    return list(dict.fromkeys(out))


def search_catalog(domain: str, query: str, limit: int = 20) -> list[dict] | None:
    """Datasets on one portal matching a query: [{id, name, description, columns}].
    None if the site isn't an open-data portal of this kind. The portal's own
    catalog is tried first, then the platform's shared catalog for that portal."""
    data = None
    for url, params in ((f"https://{domain}/api/catalog/v1", {"q": query, "only": "dataset", "limit": limit}),
                        ("https://api.us.socrata.com/api/catalog/v1",
                         {"domains": domain, "search_context": domain, "q": query, "only": "dataset",
                          "limit": limit})):
        try:
            data = _get(url, **params)
            break
        except (requests.RequestException, ValueError):
            continue
    if not isinstance(data, dict) or "results" not in data:
        return None
    out = []
    for item in (data or {}).get("results", []):
        res = item.get("resource") or {}
        if res.get("id") and res.get("name"):
            out.append({"id": res["id"], "name": res["name"], "description": res.get("description") or "",
                        "columns": res.get("columns_field_name") or [],
                        "domain": (item.get("metadata") or {}).get("domain") or domain})
    return out


def pick_datasets(domain: str, what: str, max_datasets: int = 4) -> list[dict]:
    """The most useful license / permit datasets on a portal for this industry."""
    rx = keyword_regex(what)
    kw = re.compile(rf"\b({rx})", re.I) if rx else None
    words = [w for w in re.findall(r"[a-z0-9]+", what.lower()) if len(w) > 2]
    main = max(words, key=len) if words else ""
    found: dict[str, dict] = {}
    for i, q in enumerate(CATALOG_QUERIES):
        query = q.format(kw=main).strip()
        if "{kw}" in q and not main:
            continue
        results = search_catalog(domain, query)
        if results is None:
            if i == 0:
                return []  # not an open-data portal: don't keep asking
            continue
        for d in results:
            if d["id"] in found:
                continue
            text = d["name"] + " " + d["description"]
            if not _GOOD_DATASET.search(text) or _BAD_DATASET.search(d["name"]):
                continue
            cols = _match_columns(d["columns"]) if d["columns"] else {"name": "?"}
            if "name" not in cols:
                continue  # no business name column: can't produce companies
            score = 0
            score += 10 if kw and kw.search(text) else 0
            score += 5 if re.search(r"licen[cs]|contractor", text, re.I) else 0
            score += 3 if re.search(r"permit", text, re.I) else 0
            score += 2 if "phone" in cols else 0
            score += 1 if "email" in cols or "website" in cols else 0
            d["score"] = score
            found[d["id"]] = d
        if len(found) >= max_datasets * 3:
            break
    return sorted(found.values(), key=lambda d: -d["score"])[:max_datasets]


def find_public_records(what: str, where: str, max_portals: int = 4, progress=None,
                        reachable=None) -> tuple[list[Candidate], list[str]]:
    """Candidates from license / permit datasets on the area's open-data portals.
    Returns (candidates, notes about what was searched)."""
    from .datasources import from_source
    from .discover import _resolves
    reachable = reachable or _resolves
    say = progress or (lambda m: None)
    notes: list[str] = []
    out: dict[str, Candidate] = {}
    used = 0
    for domain in portals_for(where):
        if used >= max_portals:
            break
        if not reachable(domain):
            continue
        datasets = pick_datasets(domain, what)
        if not datasets:
            continue
        used += 1
        for d in datasets:
            url = f"https://{d['domain']}/resource/{d['id']}.json"
            label = f"{d['name']} ({d['domain']})"
            try:
                rows = from_source(url, what, where, label)
            except (requests.RequestException, ValueError) as e:
                log.info("dataset %s failed: %s", url, e)
                continue
            say(f"{label}: {len(rows)} matching businesses")
            notes.append(f"{label}: {len(rows)}")
            for c in rows:
                if c.key not in out:
                    out[c.key] = c
                else:
                    out[c.key].sources |= c.sources
    # Strongest first: licensed AND permitted beats one list; then most records (most active).
    def strength(c: Candidate) -> int:
        records = sum(int(m) for s in c.sources for m in re.findall(r"\((\d+) records\)", s)) or len(c.sources)
        return len(c.sources) * 2 + records
    return sorted(out.values(), key=lambda c: -strength(c)), notes
