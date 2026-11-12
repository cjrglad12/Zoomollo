"""Things Zoomollo never stores or contacts.

1. Generic / shared inboxes: info@, careers@, jobs@, sales@, hello@ ...
2. Recruiting and staffing people: recruiters, talent acquisition, sourcers.
3. Recruiting and staffing companies: agencies, executive search, temp firms.
"""

from __future__ import annotations

import re

# Local parts (or their first word) that mean a shared inbox, not a person.
GENERIC_LOCALPARTS = {
    "info", "information", "sales", "support", "help", "helpdesk", "hello", "hi", "hey", "contact",
    "contactus", "admin", "administrator", "office", "frontdesk", "reception", "team", "careers",
    "career", "jobs", "job", "recruiting", "recruitment", "recruiter", "recruiters", "talent",
    "hiring", "hr", "humanresources", "people", "employment", "resume", "resumes", "apply",
    "applications", "applicants", "press", "media", "pr", "news", "marketing", "billing", "accounts",
    "accounting", "ap", "ar", "invoices", "invoice", "payables", "receivables", "finance", "legal",
    "privacy", "security", "abuse", "compliance", "partners", "partnerships", "service", "services",
    "customerservice", "customercare", "care", "enquiries", "enquiry", "inquiries", "inquiry",
    "webmaster", "web", "website", "feedback", "orders", "order", "general", "mail", "email",
    "estimates", "estimating", "quotes", "quote", "bids", "bid", "dispatch", "scheduling",
    "schedule", "appointments", "booking", "bookings", "service", "warranty", "parts",
    "operations", "ops", "it", "tech", "techsupport", "noreply", "donotreply", "postmaster",
    "hostmaster", "root", "all", "staff", "everyone", "company", "corporate", "main", "host",
    "events", "community", "social", "membership", "members", "donate", "donations", "giving",
    "volunteer", "admissions", "registrar", "studio", "shop", "store", "online", "ecommerce",
    "subscribe", "newsletter", "unsubscribe", "bookkeeping", "payroll", "benefits", "safety",
    "quality", "purchasing", "procurement", "vendors", "vendor", "supplier", "suppliers",
}

_RECRUITING_TITLE = re.compile(
    r"\b(recruit\w*|talent acquisition|talent partner|talent sourc\w*|talent advisor|talent scout|"
    r"sourcer|sourcing specialist|staffing|headhunt\w*|executive search|placement|"
    r"technical recruiter|campus recruit\w*|ta (partner|manager|lead|specialist|coordinator))\b", re.I)

_STAFFING_COMPANY = re.compile(
    r"\b(staffing|recruiting (firm|agency|company|partner|solutions|services)|recruitment "
    r"(agency|firm|company|solutions|services|consultancy)|executive search|search firm|headhunt\w*|"
    r"talent (acquisition|solutions) (firm|agency|company|services)|temp(orary)? (agency|staffing|"
    r"employment)|temp[- ]to[- ]hire|placement (agency|firm|services)|employment agency|"
    r"staff augmentation|workforce solutions|contract staffing|direct hire placement|"
    r"recruitment process outsourcing|rpo provider)\b", re.I)

# Big staffing / search firms (company domain or email domain).
STAFFING_DOMAINS = {
    "roberthalf.com", "randstad.com", "randstadusa.com", "adecco.com", "adeccousa.com", "manpower.com",
    "manpowergroup.com", "manpowergroupusa.com", "kellyservices.com", "aerotek.com", "teksystems.com",
    "insightglobal.com", "allegisgroup.com", "kforce.com", "expresspros.com", "spherion.com",
    "apexsystems.com", "kornferry.com", "heidrick.com", "spencerstuart.com", "russellreynolds.com",
    "egonzehnder.com", "hays.com", "michaelpage.com", "pagegroup.com", "aston-carter.com",
    "astoncarter.com", "actalentservices.com", "actalent.com", "volt.com", "trueblue.com",
    "rgp.com", "robertwalters.com", "ajilon.com",
    "peopleready.com", "labor-ready.com", "tandym.com", "beaconhillstaffing.com", "lhh.com",
    "modis.com", "akkodis.com", "cybercoders.com", "jobot.com", "motionrecruitment.com",
    "addisongroup.com", "vacocareers.com", "lucasgroup.com", "hirequest.com", "staffmark.com",
    "accountemps.com", "officeteam.com", "nesfircroft.com", "yoh.com", "procom.ca", "collabera.com",
    "experis.com", "creativecircle.com", "atrium.com", "medix.com", "amnhealthcare.com",
    "ayahealthcare.com", "crosscountry.com", "cross-country.com", "tradesmeninternational.com",
    "tradesmen.com", "pridestaff.com", "expresspros.net", "snelling.com", "westaff.com",
}


def is_generic_email(email: str) -> bool:
    local = email.split("@", 1)[0].lower()
    if re.sub(r"[^a-z]", "", local) in GENERIC_LOCALPARTS:
        return True
    first = re.split(r"[._+\-0-9]", local)[0]
    return first in GENERIC_LOCALPARTS


def is_staffing_domain(domain: str | None) -> bool:
    d = (domain or "").lower().removeprefix("www.")
    return any(d == s or d.endswith("." + s) for s in STAFFING_DOMAINS)


def is_staffing_email(email: str) -> bool:
    return is_staffing_domain(email.rsplit("@", 1)[-1])


def is_excluded_email(email: str) -> bool:
    return is_generic_email(email) or is_staffing_email(email)


def is_recruiting_title(title: str | None) -> bool:
    return bool(title) and bool(_RECRUITING_TITLE.search(title))


def is_unwanted_title(title: str | None) -> bool:
    """Recruiting/staffing people and customer service or tech support reps."""
    return is_recruiting_title(title) or is_support_title(title)


# Customer service / tech support: numbers and people that never reach a hiring manager.
SUPPORT_LABEL = re.compile(
    r"\b(?:customer|client|consumer|member|patient|guest)s?\s+(?:service|services|support|care|success|experience)\b"
    r"|\btech(?:nical)?\s+support\b|\bit\s+support\b|\bhelp\s?desk\b|\bservice\s+desk\b|\bsupport\s+desk\b"
    r"|\bsupport\s+(?:line|hotline|center|centre|team|portal|number|phone)\b|\b24\s*/\s*7\s+support\b|\b(?:for|call|contact|get)\s+support\b"
    r"|\bsupport\s*:|\bcall\s+cent(?:er|re)\b|\bcontact\s+cent(?:er|re)\b"
    r"|\bbilling\s+(?:department|dept|questions|inquiries|support|line|team|help)\b"
    r"|\bclaims\s+(?:department|dept|line|center|hotline|reporting)\b|\b(?:file|report)\s+a\s+claim\b"
    r"|\border\s+status\b|\breturns\s+(?:department|line|center)\b|\bwarranty\s+(?:service|claims|department|line|support)\b"
    r"|\bservice\s+requests?\b|\bschedule\s+(?:a\s+)?service\b"
    r"|\breport\s+an?\s+(?:outage|issue|problem)\b|\b(?:24[- ]hour|emergency|after[- ]hours)\s+(?:service|line|support|dispatch)\b"
    r"|\bdispatch\b|\bcustomer\s+line\b", re.I)
# Labels that mean the number after them is an office line, not support.
OFFICE_LABEL = re.compile(r"\b(?:main|office|corporate|headquarters|hq|reception|front\s+desk|switchboard|"
                          r"general|administration|admin|local)\b", re.I)
SUPPORT_PATH = re.compile(r"/(?:support|customer-?service|customer-?care|customer-?support|help|help-?desk|"
                          r"help-?center|tech-?support|technical-?support|contact-?support|billing|claims|"
                          r"service-?request|faqs?)(?:/|$|\.|-)", re.I)
SUPPORT_HOST = re.compile(r"^(?:support|help|helpdesk|service|care|customercare|billing)\.", re.I)

_SUPPORT_AREA = re.compile(
    r"\b(?:customer|client|consumer|member|patient|guest)s?\s+(?:service|services|support|care|success|experience)\b"
    r"|\btech(?:nical)?\s+support\b|\bit\s+support\b|\bdesktop\s+support\b|\bproduct\s+support\b"
    r"|\bapplication\s+support\b|\bhelp\s?desk\b|\bservice\s+desk\b|\bsupport\s+desk\b|\bcall\s+cent(?:er|re)\b"
    r"|\bcontact\s+cent(?:er|re)\b|\bcsr\b|\bdispatcher\b|\bbilling\s+(?:rep|representative|specialist|clerk)\b", re.I)
_LEADS_A_TEAM = re.compile(r"\b(?:manager|director|vp|vice\s+president|head|chief|president|owner|partner|"
                           r"supervisor|controller|officer)\b", re.I)


def is_support_page(url: str | None) -> bool:
    """A help, support or billing page: its phone numbers go to customer service."""
    from urllib.parse import urlparse
    u = urlparse(url or "")
    return bool(SUPPORT_PATH.search(u.path or "") or SUPPORT_HOST.match(u.netloc.lower().removeprefix("www.")))


def is_support_title(title: str | None) -> bool:
    """CSRs, tech support reps and the like: they don't hire and can't put you through.
    Whoever runs that team (a Customer Service Manager, say) is kept, except Customer
    Success Managers, who look after customers rather than a team."""
    if not title or not _SUPPORT_AREA.search(title):
        return False
    if re.search(r"\b(?:customer|client)\s+success\s+manager\b", title, re.I):
        return True
    return not _LEADS_A_TEAM.search(title)


# Words that make the end of a title an organisation's name, e.g.
# "Chief Growth Officer, Ideal Credit Union" (a customer quoted on the site).
_ORG_WORDS = re.compile(
    r"\b(credit union|bank|bancorp|bancshares|banking company|national association|savings|"
    r"inc|incorporated|llc|l\.l\.c|llp|corp|corporation|co|company|ltd|limited|plc|gmbh|"
    r"university|college|hospital|health system|medical center|school district|isd|"
    r"county)\b\.?\s*$|^(the )?(bank|university|city|county|state|department) of\b", re.I)
_NA = re.compile(r"\bN\.\s?A\.?$")  # "Bank of X, N.A." (capitals only)
_STOP = {"the", "of", "and", "inc", "llc", "co", "corp", "company", "group", "com", "www"}


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in _STOP and len(w) > 1}


def works_elsewhere(title: str | None, company_name: str | None = None, domain: str | None = None) -> bool:
    """True when a title names another organisation ("CFO, Ideal Credit Union",
    "Board member at Acme Bank"): a customer, partner or board member quoted
    on the site, not someone who works at this company."""
    if not title:
        return False
    parts = re.split(r",\s+|\s+at\s+|\s+[|@–—]\s+|\s+-\s+", title.strip())
    tails = [p.strip(" .") for p in parts[1:] if p.strip(" .")]
    if not tails:
        return False
    own = _words(company_name or "") | _words((domain or "").split(".")[0])
    stem = re.sub(r"[^a-z0-9]", "", (domain or "").split(".")[0].lower())
    for tail in tails:
        if not (_ORG_WORDS.search(tail) or _NA.search(tail)):
            continue
        w = _words(tail)
        squashed = re.sub(r"[^a-z0-9]", "", tail.lower())
        if (own and w & own) or (len(stem) >= 4 and stem in squashed):
            continue  # it's this company's own name
        return True
    return False


def staffing_signal(*texts: str | None, domain: str | None = None) -> str | None:
    """Why this looks like a staffing/recruiting company, or None."""
    if is_staffing_domain(domain):
        return f"known staffing firm ({domain})"
    for t in texts:
        if t and (m := _STAFFING_COMPANY.search(t)):
            return f'"{m.group(0)}"'
    return None
