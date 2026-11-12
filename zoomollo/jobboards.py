"""Public job-board APIs (Greenhouse, Lever, Ashby, Workable, Workday, Oracle, UKG/UltiPro, iCIMS,
Breezy, ClearCompany, ADP Workforce Now, JazzHR, Paylocity).

These are the official, unauthenticated JSON feeds each ATS publishes for a
company's careers page, so no HTML scraping or ToS-breaking is involved.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field

import requests

from .crawler import USER_AGENT
from .extract import extract_text


@dataclass
class Job:
    board: str
    company_slug: str
    external_id: str
    title: str
    location: str | None
    url: str | None
    posted_at: str | None
    description: str = ""
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)


def _get(url: str, **params):
    r = requests.get(url, params=params, timeout=20, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return r.json()


def greenhouse(slug: str) -> list[Job]:
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", content="true")
    return [
        Job("greenhouse", slug, str(j["id"]), j.get("title", ""),
            (j.get("location") or {}).get("name"), j.get("absolute_url"),
            j.get("updated_at"), html.unescape(j.get("content") or ""))
        for j in data.get("jobs", [])
    ]


def lever(slug: str) -> list[Job]:
    data = _get(f"https://api.lever.co/v0/postings/{slug}", mode="json")
    jobs = []
    for j in data:
        parts = [j.get("descriptionPlain") or ""]
        for lst in j.get("lists") or []:
            parts.append(lst.get("text", "") + " " + (lst.get("content") or ""))
        parts.append(j.get("additionalPlain") or "")
        jobs.append(Job("lever", slug, j["id"], j.get("text", ""),
                        (j.get("categories") or {}).get("location"), j.get("hostedUrl"),
                        str(j.get("createdAt") or ""), "\n".join(parts)))
    return jobs


def ashby(slug: str) -> list[Job]:
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    return [
        Job("ashby", slug, j.get("id") or j.get("jobUrl", ""), j.get("title", ""),
            j.get("location"), j.get("jobUrl"), j.get("publishedAt"),
            j.get("descriptionHtml") or j.get("descriptionPlain") or "")
        for j in data.get("jobs", [])
    ]


def workable(slug: str) -> list[Job]:
    data = _get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", details="true")
    jobs = []
    for j in data.get("jobs", []):
        loc = ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x)
        jobs.append(Job("workable", slug, j.get("shortcode", ""), j.get("title", ""),
                        loc or None, j.get("url"), j.get("published_on"),
                        j.get("description", "")))
    return jobs


def workday(slug: str, max_jobs: int = 300, max_details: int = 60) -> list[Job]:
    """Workday careers sites (used by many large employers). slug is 'tenant.wdN/site',
    e.g. 'alkami.wd12/Alkami' for alkami.wd12.myworkdayjobs.com/Alkami. Uses the same
    public JSON feed the careers page itself loads. Full descriptions (where recruiter
    names and emails appear) are read for the newest max_details jobs."""
    host_part, _, site = slug.partition("/")
    tenant, _, wd = host_part.partition(".")
    if not (tenant and wd and site):
        raise ValueError("Workday slug must look like tenant.wd1/SiteName")
    host = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{tenant}/{site}"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", "Content-Type": "application/json"}
    postings: list[dict] = []
    seen: set[str] = set()
    total, offset = None, 0
    while len(postings) < max_jobs and (total is None or offset < total):
        r = requests.post(f"{api}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": offset,
                                               "searchText": ""}, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
        if total is None:
            total = data.get("total") or 0  # only the first page reports the real total
        page = data.get("jobPostings") or []
        new = [p for p in page if p.get("externalPath") not in seen]
        if not new:
            break  # Workday repeats the last page past the end
        seen.update(p.get("externalPath") for p in new)
        postings += new
        offset += 20
    jobs = []
    for i, p in enumerate(postings[:max_jobs]):
        path = p.get("externalPath") or ""
        url = f"{host}/{site}{path}" if path else None
        ext_id = (p.get("bulletFields") or [path])[0] or path
        title, loc, posted, desc = p.get("title", ""), p.get("locationsText"), None, ""
        if i < max_details and path:
            try:
                info = requests.get(api + path, headers=headers, timeout=20).json().get("jobPostingInfo") or {}
                desc, posted = info.get("jobDescription") or "", info.get("startDate")
                loc = info.get("location") or loc
                url = info.get("externalUrl") or url
                ext_id = info.get("jobReqId") or ext_id
            except (requests.RequestException, ValueError):
                pass
        jobs.append(Job("workday", slug, str(ext_id), title, loc, url, posted, html.unescape(desc)))
    return jobs


def oracle(slug: str, max_jobs: int = 1500) -> list[Job]:
    """Oracle Recruiting Cloud (BDO and many large firms). slug is 'host/SiteNumber', e.g.
    'ebqb.fa.us2.oraclecloud.com/BDOExperiencedCareers'. Uses the public feed the careers page loads."""
    host, _, site = slug.partition("/")
    if not (host.endswith(".oraclecloud.com") and site):
        raise ValueError("Oracle slug must look like xxxx.fa.us2.oraclecloud.com/SiteNumber")
    api = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    jobs: list[Job] = []
    offset = 0
    while len(jobs) < max_jobs:
        r = requests.get(api, params={"onlyData": "true", "expand": "requisitionList",
                                      "finder": f"findReqs;siteNumber={site},limit=100,offset={offset},"
                                                "sortBy=POSTING_DATES_DESC"},
                         headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=30)
        r.raise_for_status()
        items = r.json().get("items") or [{}]
        reqs = items[0].get("requisitionList") or []
        for q in reqs:
            jid = str(q.get("Id", ""))
            # Keyed by host: a firm's sites (e.g. entry-level / experienced) can list the same openings.
            jobs.append(Job("oracle", slug, f"{host}:{jid}", q.get("Title", ""), q.get("PrimaryLocation"),
                            f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{jid}",
                            q.get("PostedDate"), q.get("ShortDescriptionStr") or ""))
        offset += 100
        if not reqs or offset >= (items[0].get("TotalJobsCount") or 0):
            break
    return jobs[:max_jobs]


def ultipro(slug: str, max_jobs: int = 1500) -> list[Job]:
    """UKG Pro / UltiPro Recruiting (Weaver and many mid-size firms). slug is
    'recruiting2.ultipro.com/TENANT/board-guid' (the job board link without /JobBoard/)."""
    parts = slug.split("/")
    if len(parts) != 3 or "ultipro.com" not in parts[0]:
        raise ValueError("UltiPro slug must look like recruiting2.ultipro.com/TENANT/board-guid")
    host, tenant, board = parts
    base = f"https://{host}/{tenant}/JobBoard/{board}"
    jobs: list[Job] = []
    skip = 0
    while len(jobs) < max_jobs:
        r = requests.post(base + "/JobBoardView/LoadSearchResults",
                          json={"opportunitySearch": {"Top": 100, "Skip": skip, "QueryString": "",
                                                      "OrderBy": [], "Filters": []}},
                          headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=30)
        r.raise_for_status()
        data = r.json()
        opps = data.get("opportunities") or []
        for o in opps:
            locs = []
            for loc in o.get("Locations") or []:
                a = loc.get("Address") or {}
                city = (a.get("City") or "").title()
                state = (a.get("State") or {}).get("Code")
                locs.append(", ".join(x for x in [city, state] if x) or loc.get("LocalizedName") or "")
            jobs.append(Job("ultipro", slug, o.get("Id", ""), o.get("Title", ""), "; ".join(x for x in locs if x) or None,
                            f"{base}/OpportunityDetail?opportunityId={o.get('Id', '')}", o.get("PostedDate"),
                            o.get("BriefDescription") or ""))
        skip += 100
        if not opps or skip >= (data.get("totalCount") or 0):
            break
    return jobs[:max_jobs]


def icims(slug: str, max_pages: int = 10) -> list[Job]:
    """iCIMS careers portals (Critical Start, Principal and many others). slug is the portal
    host, e.g. 'careers-criticalstart.icims.com'. Reads the portal's own search pages."""
    import re
    host = slug if "." in slug else f"careers-{slug}.icims.com"
    if not host.endswith(".icims.com"):
        raise ValueError("iCIMS slug must be a host like careers-company.icims.com")
    jobs: list[Job] = []
    seen: set[str] = set()
    for page in range(max_pages):
        r = requests.get(f"https://{host}/jobs/search", params={"ss": 1, "in_iframe": 1, "pr": page},
                         headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        cards = re.split(r'<li class="iCIMS_JobCardItem', r.text)[1:]
        new = 0
        for card in cards:
            m = re.search(r'href="(https://[^"]+/jobs/(\d+)/[^"]+?)(?:\?in_iframe=1)?"[^>]*>.*?<h3[^>]*>\s*([^<]+?)\s*</h3>',
                          card, re.S)
            if not m or m.group(2) in seen:
                continue
            seen.add(m.group(2))
            new += 1
            loc = re.search(r'Job Locations</span>\s*<span[^>]*>\s*([^<]+?)\s*</span>', card)
            locs = [re.sub(r"^US-([A-Z]{2})-(.+)$", r"\2, \1", x.strip()) for x in (loc.group(1).split("|") if loc else [])]
            desc = re.search(r'class="col-xs-12 description">\s*([^<]*)', card)
            jobs.append(Job("icims", host, f"{host}:{m.group(2)}", html.unescape(m.group(3)),
                            "; ".join(locs) or None, m.group(1), None,
                            html.unescape(desc.group(1).strip()) if desc else ""))
        if not new:
            break
    return jobs


_GUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


def _need_guid(slug: str, board: str) -> str:
    import re
    if not re.match(_GUID, slug or ""):
        raise ValueError(f"{board} needs the company's ID from its careers link")
    return slug


def breezy(slug: str) -> list[Job]:
    """Breezy HR (Bartlett Cocke and many mid-size firms): slug.breezy.hr/json."""
    data = _get(f"https://{slug}.breezy.hr/json")
    jobs = []
    for j in data if isinstance(data, list) else []:
        locs = [x.get("name") for x in (j.get("locations") or [j.get("location") or {}]) if x.get("name")]
        if (j.get("location") or {}).get("is_remote"):
            locs.append("Remote")
        jobs.append(Job("breezy", slug, j.get("id", ""), j.get("name", ""), "; ".join(dict.fromkeys(locs)) or None,
                        j.get("url"), j.get("published_date"),
                        " ".join(x for x in [j.get("salary") or "", (j.get("type") or {}).get("name", "")] if x)))
    return jobs


def clearcompany(slug: str) -> list[Job]:
    """ClearCompany career sites (Pogue Construction, ...). slug is the site ID (a GUID)."""
    site = _need_guid(slug, "ClearCompany")
    jobs, page, seen = [], 0, set()
    while page < 20:
        data = _get(f"https://careers-api.clearcompany.com/v1/{site}", pageIndex=page)
        rows = [j for j in data.get("results") or [] if j.get("id") not in seen]
        for j in rows:
            if j.get("id") in seen:
                continue
            seen.add(j.get("id"))
            # The office is where the job is; "location" is often just headquarters.
            st = next((x.get("subdivision") for x in j.get("locations") or [] if x.get("subdivision")), None)
            office = (j.get("officeName") or "").strip()
            loc = f"{office}, {st}" if office and st else office or j.get("location") or ""
            lo, hi = (j.get("salaryRangeLow") or "0"), (j.get("salaryRangeHigh") or "0")
            pay = f"Salary ${lo}–${hi}. " if str(hi) not in ("0", "", "None") else ""
            jobs.append(Job("clearcompany", site, j.get("id", ""), j.get("positionTitle") or j.get("title", ""),
                            loc or None, j.get("applyLink") or f"https://careers-content.clearcompany.com/?siteId={site}",
                            j.get("postedDate"), pay + html.unescape(extract_plain(j.get("description")))))
        total = data.get("totalCount") or 0
        if not rows or len(jobs) >= total:
            break
        page += 1
    return jobs


def extract_plain(markup: str | None) -> str:
    import re
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", markup or "")).strip()


def adp(slug: str, max_jobs: int = 500) -> list[Job]:
    """ADP Workforce Now career centers. slug is the cid GUID from the careers link."""
    cid = _need_guid(slug, "ADP")
    url = "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
    jobs, skip = [], 1
    while len(jobs) < max_jobs:
        data = _get(url, cid=cid, lang="en_US", locale="en_US", **{"$top": 50, "$skip": skip})
        rows = data.get("jobRequisitions") or []
        for j in rows:
            locs = []
            for l in j.get("requisitionLocations") or []:
                a = l.get("address") or {}
                city, st = a.get("cityName"), (a.get("countrySubdivisionLevel1") or {}).get("codeValue")
                locs.append(", ".join(x for x in [city, st] if x) or ((l.get("nameCode") or {}).get("shortName") or "").strip())
            jid = j.get("itemID", "")
            jobs.append(Job("adp", cid, jid, j.get("requisitionTitle", ""), "; ".join(x for x in locs if x) or None,
                            f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={cid}"
                            f"&jobId={jid}", (j.get("postDate") or "")[:10] or None,
                            (j.get("workLevelCode") or {}).get("shortName", "")))
        total = (data.get("meta") or {}).get("totalNumber") or 0
        if not rows or len(jobs) >= total:
            break
        skip += len(rows)
    return jobs


def jazzhr(slug: str) -> list[Job]:
    """JazzHR (slug.applytojob.com): reads the public job list."""
    import re
    r = requests.get(f"https://{slug}.applytojob.com/apply/jobs/", headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    jobs = []
    for row in re.split(r'<tr id="row_job_', r.text)[1:]:
        m = re.search(r'class="job_title_link" href="(/apply/jobs/details/([A-Za-z0-9]+))[^"]*">\s*([^<]+?)\s*</a>', row)
        if not m:
            continue
        tds = re.findall(r"<td>(.*?)</td>", row, re.S)
        loc = extract_plain(tds[1]) if len(tds) > 1 else ""
        dept = re.search(r'resumator_department">([^<]+)', row)
        jobs.append(Job("jazzhr", slug, m.group(2), html.unescape(m.group(3)), loc or None,
                        f"https://{slug}.applytojob.com{m.group(1)}", None, dept.group(1).strip() if dept else ""))
    return jobs


def jobvite(slug: str, max_pages: int = 30) -> list[Job]:
    """Jobvite career sites (jobs.jobvite.com/slug): reads the public job list, page by page."""
    import re
    jobs: list[Job] = []
    seen: set[str] = set()
    for page in range(max_pages):
        r = requests.get(f"https://jobs.jobvite.com/{slug}/search", params={"p": page},
                         headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        new = 0
        for m in re.finditer(r'class="jv-job-list-name">\s*<a href="(/[^"/]+/job/([A-Za-z0-9]+))"[^>]*>\s*([^<]+?)\s*</a>'
                             r'.*?class="jv-job-list-location">(.*?)</td>', r.text, re.S):
            if m.group(2) in seen:
                continue
            seen.add(m.group(2))
            new += 1
            loc = re.sub(r"\s*,\s*", ", ", extract_plain(m.group(4)))
            if re.fullmatch(r"\d+ Locations?", loc):
                loc = ""  # "4 Locations" isn't a place; the company's city is used
            jobs.append(Job("jobvite", slug, m.group(2), html.unescape(m.group(3)), loc or None,
                            f"https://jobs.jobvite.com{m.group(1)}", None))
        if not new:
            break
    return jobs


def paylocity(slug: str) -> list[Job]:
    """Paylocity recruiting pages. slug is the company GUID from recruiting.paylocity.com/recruiting/jobs/All/GUID."""
    import json
    import re
    guid = _need_guid(slug, "Paylocity")
    r = requests.get(f"https://recruiting.paylocity.com/recruiting/jobs/All/{guid}",
                     headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    m = re.search(r"window\.pageData\s*=\s*(\{.*?\});\s*\n", r.text, re.S)
    if not m:
        return []
    jobs = []
    for j in json.loads(m.group(1)).get("Jobs") or []:
        if j.get("IsInternal"):
            continue
        jl = j.get("JobLocation") or {}
        loc = j.get("LocationName") or ", ".join(x for x in [jl.get("City"), jl.get("State")] if x)
        jid = str(j.get("JobId") or j.get("Id") or "")
        jobs.append(Job("paylocity", guid, jid, j.get("JobTitle") or j.get("Title", ""), loc or None,
                        f"https://recruiting.paylocity.com/Recruiting/Jobs/Details/{jid}",
                        (j.get("PublishedDate") or "")[:10] or None, j.get("HiringDepartment") or ""))
    return jobs


BOARDS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workable": workable, "workday": workday,
          "oracle": oracle, "ultipro": ultipro, "icims": icims, "breezy": breezy, "clearcompany": clearcompany,
          "adp": adp, "jazzhr": jazzhr, "paylocity": paylocity, "jobvite": jobvite}


def fetch_jobs(board: str, slug: str, region: str = "US") -> list[Job]:
    if board not in BOARDS:
        raise ValueError(f"Unknown board '{board}'. Use one of: {', '.join(BOARDS)}")
    jobs = BOARDS[board](slug)
    for j in jobs:
        j.emails, j.phones = extract_text(j.description, region)
    return jobs


def detect_board(slug: str) -> list[tuple[str, int]]:
    """Try every board for a slug; returns [(board, job_count)] that answered."""
    found = []
    for name, fn in BOARDS.items():
        try:
            n = len(fn(slug))
        except (requests.RequestException, ValueError):
            continue
        if n:
            found.append((name, n))
    return found
