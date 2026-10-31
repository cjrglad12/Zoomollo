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


def staffing_signal(*texts: str | None, domain: str | None = None) -> str | None:
    """Why this looks like a staffing/recruiting company, or None."""
    if is_staffing_domain(domain):
        return f"known staffing firm ({domain})"
    for t in texts:
        if t and (m := _STAFFING_COMPANY.search(t)):
            return f'"{m.group(0)}"'
    return None
