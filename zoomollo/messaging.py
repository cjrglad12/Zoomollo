"""Text outreach through WhatsApp (or SMS) with message templates.

Sending is one tap per message: Zoomollo fills in the template and opens
WhatsApp's official click-to-chat link (https://wa.me/<number>?text=...) or
your phone's text app with the message ready; you press Send. No WhatsApp
account linking, no automation and no outside service. Each message opened is
logged so you can see who you've contacted.

Templates use fields in curly brackets:
  {first_name} {last_name} {name} {title} {company} {city} {role}
  {my_name} {my_company}
{role} is the open role the person most likely owns. {my_name} / {my_company}
are set on the Messages tab (or MY_NAME / MY_COMPANY in zoomollo-settings.txt).
"""

from __future__ import annotations

import os
import re
from urllib.parse import quote

from .db import DB, now

FIELDS = ["first_name", "last_name", "name", "title", "company", "city", "role", "my_name", "my_company"]

# Used when a field is unknown for a person, so messages still read naturally.
FALLBACKS = {"first_name": "there", "name": "there", "company": "your company", "role": "new team member",
             "title": "", "city": "", "last_name": "", "my_name": "", "my_company": ""}

STARTER_TEMPLATES = [
    ("Open role intro",
     "Hi {first_name}, this is {my_name} with {my_company}. I saw {company} is hiring a {role} in {city}. "
     "I work with experienced people in that space and have a couple who could be a strong fit. "
     "Open to a quick call this week? If not, reply STOP and I won't reach out again."),
    ("Quick follow-up",
     "Hi {first_name}, {my_name} here again. Just following up on the {role} opening at {company}. "
     "Happy to send over a profile or two if that's helpful. Reply STOP to opt out."),
    ("General intro",
     "Hi {first_name}, I'm {my_name} with {my_company}. I help teams like {company} find great people "
     "when they're growing. Would it be worth a short conversation? Reply STOP to opt out."),
]

_FIELD_RE = re.compile(r"\{(\w+)\}")


def ensure_starters(db: DB) -> None:
    if db.conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 0:
        for name, body in STARTER_TEMPLATES:
            db.save_template(name, body)


def unknown_fields(body: str) -> list[str]:
    return sorted({f for f in _FIELD_RE.findall(body) if f not in FIELDS})


def person_fields(db: DB, person_id: int | None = None, company_id: int | None = None) -> dict[str, str]:
    """Values for every template field for a person (or just a company)."""
    vals: dict[str, str] = {k: "" for k in FIELDS}
    vals["my_name"] = db.get_setting("my_name") or os.environ.get("MY_NAME", "")
    vals["my_company"] = db.get_setting("my_company") or os.environ.get("MY_COMPANY", "")
    if person_id is not None:
        p = db.conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
        if p:
            company_id = p["company_id"]
            parts = p["name"].split()
            vals.update(name=p["name"], first_name=parts[0], last_name=parts[-1] if len(parts) > 1 else "",
                        title=p["title"] or "")
            if p["hiring_for"]:
                vals["role"] = p["hiring_for"].split(";")[0].split(" (+")[0].strip()
    if company_id is not None:
        co = db.company_row(company_id)
        if co:
            vals["company"] = co["name"] or co["domain"] or ""
            vals["city"] = (co["city"] or "").split(",")[0].strip()
        if not vals["role"]:
            job = db.conn.execute("SELECT title FROM jobs WHERE company_id = ? ORDER BY first_seen DESC LIMIT 1",
                                  (company_id,)).fetchone()
            if job:
                vals["role"] = job["title"]
    return vals


def render(body: str, values: dict[str, str]) -> tuple[str, list[str]]:
    """Fill a template. Returns (text, fields that used a fallback)."""
    missing: list[str] = []

    def sub(m):
        key = m.group(1)
        if key not in FIELDS:
            return m.group(0)
        v = (values.get(key) or "").strip()
        if not v:
            missing.append(key)
            v = FALLBACKS.get(key, "")
        return v

    text = _FIELD_RE.sub(sub, body)
    text = re.sub(r"[ \t]{2,}", " ", text)
    # Your name/company not set yet: "this is  with ." -> "."
    for _ in range(2):
        text = re.sub(r"[ \t]*\b(?:this is|I'm|I am|with|from|in|at)[ \t]*(?=[,.!?])", "", text)
    text = re.sub(r",\s*([.!?])", r"\1", text)
    text = re.sub(r"\s+([,.!?])", r"\1", text)
    text = re.sub(r"([.!?])\.+", r"\1", text)
    # "a HVAC Technician" -> "an HVAC Technician", "a Engineer" -> "an Engineer"
    text = re.sub(r"\b([Aa]) (?=(?:[AEIOaeio]\w|[FHLMNRSX][A-Z0-9]{1,5}\b))", lambda m: m.group(1) + "n ", text)
    return text.strip(), sorted(set(missing))


def digits(phone: str) -> str:
    """'+1 (512) 555-0134' -> '15125550134' (country code included)."""
    d = re.sub(r"\D", "", phone or "")
    if len(d) == 10:  # US number without country code
        d = "1" + d
    return d


def whatsapp_link(phone: str, text: str) -> str:
    return f"https://wa.me/{digits(phone)}?text={quote(text)}"


def sms_link(phone: str, text: str) -> str:
    # "?&body=" works on both iPhone and Android.
    return f"sms:+{digits(phone)}?&body={quote(text)}"


def log_message(db: DB, phone: str, text: str, channel: str, person_id: int | None = None,
                company_id: int | None = None, template_id: int | None = None) -> bool:
    """Record an opened message. Returns False if the number has opted out."""
    e164 = "+" + digits(phone)
    if db.is_suppressed(e164):
        return False
    db.conn.execute(
        "INSERT INTO messages (person_id, company_id, phone, channel, template_id, body, sent_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", (person_id, company_id, e164, channel, template_id, text, now()))
    db.commit()
    return True


def last_messaged(db: DB, phone: str) -> str | None:
    row = db.conn.execute("SELECT sent_at FROM messages WHERE phone = ? ORDER BY sent_at DESC LIMIT 1",
                          ("+" + digits(phone),)).fetchone()
    return row["sent_at"] if row else None


def outreach_list(db: DB, q: str = "", hiring_only: bool = True, include_messaged: bool = False,
                  limit: int = 200) -> list[dict]:
    """People with their own phone number, for stepping through outreach."""
    out = []
    for p in db.people(q=q, hiring_only=hiring_only, limit=10_000):
        if not p["phone"] or db.is_suppressed("+" + digits(p["phone"])):
            continue
        when = last_messaged(db, p["phone"])
        if when and not include_messaged:
            continue
        out.append({**dict(p), "last_messaged": when})
        if len(out) >= limit:
            break
    return out
