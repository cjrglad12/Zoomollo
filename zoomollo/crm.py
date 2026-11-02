"""Client pipeline, follow-up sequences and email outreach.

Pipeline: one card per company, moving through STAGES, with a main contact,
a next step + due date, notes and a timeline (activities).

Sequences: ordered steps ("day 0 email, day 3 text, day 7 email...").
Enrolling a person puts their steps in the Due list on the right days.
Replies stop the sequence; "unsubscribe" replies block the address.

Email is sent from YOUR mailbox, never through a sending service:
  * no setup: "Open in email app" (a mailto: link with everything filled in)
  * optional: SMTP_* settings (your own email account, app password) to send
    from Zoomollo, and IMAP_HOST to read replies and bounces from your inbox.
Commercial email needs your mailing address and an opt-out line (US CAN-SPAM),
so both are added to every email.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import os
import re
import smtplib
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from urllib.parse import quote

from . import messaging
from .db import DB, now
from .offlimits import status as offlimits_status

STAGES = [
    ("prospect", "Prospect"), ("contacted", "Contacted"), ("replied", "Replied"), ("meeting", "Meeting"),
    ("agreement", "Fee agreement"), ("job_order", "Job order"), ("placement", "Placement"),
    ("not_now", "Not now"), ("lost", "Lost"),
]
STAGE_NAMES = dict(STAGES)
ORDER = {k: i for i, (k, _) in enumerate(STAGES)}

STARTER_EMAILS = [
    ("Email: open role intro", "{role} at {company}",
     "Hi {first_name},\n\nI noticed {company} is hiring an {role}. I recruit for roles like this in {city} "
     "and know a few people who'd be a strong fit and are open to a move.\n\n"
     "Would a 10-minute call this week be useful? If you're already covered, no problem at all.\n\n"
     "Best,\n{my_name}"),
    ("Email: follow-up", "Re: {role} at {company}",
     "Hi {first_name},\n\nFollowing up on my note about the {role} opening. Happy to send one or two "
     "anonymised profiles so you can see if they're worth a conversation.\n\nBest,\n{my_name}"),
    ("Email: last note", "Close the loop on {role}?",
     "Hi {first_name},\n\nI don't want to clutter your inbox, so this is my last note on the {role} role. "
     "If hiring help would be useful now or later, just reply and I'll take it from there.\n\n"
     "Thanks,\n{my_name}"),
]

STARTER_SEQUENCE = ("Hiring manager follow-up", [
    (0, "email", "Email: open role intro"),
    (3, "text", "Quick follow-up"),
    (7, "email", "Email: follow-up"),
    (14, "email", "Email: last note"),
])

UNSUBSCRIBE = re.compile(r"\b(unsubscribe|remove me|take me off|stop emailing|do not (email|contact)|"
                         r"not interested,? (please )?stop|opt[ -]?out)\b", re.I)
BOUNCE_FROM = re.compile(r"mailer-daemon|postmaster|mail delivery", re.I)
BOUNCE_SUBJECT = re.compile(r"undeliver|delivery status|delivery fail|returned mail|failure notice|"
                            r"address not found|could not be delivered", re.I)


class OffLimits(Exception):
    pass


def today() -> str:
    return date.today().isoformat()


def log(db: DB, company_id: int | None, kind: str, body: str, person_id: int | None = None) -> None:
    db.conn.execute("INSERT INTO activities (company_id, person_id, kind, body, created_at) VALUES (?, ?, ?, ?, ?)",
                    (company_id, person_id, kind, body, now()))


# --- pipeline ---------------------------------------------------------------------

def card(db: DB, company_id: int):
    return db.conn.execute("SELECT * FROM pipeline WHERE company_id = ?", (company_id,)).fetchone()


def add_to_pipeline(db: DB, company_id: int, contact_id: int | None = None, stage: str = "prospect",
                    force: bool = False) -> int:
    st = offlimits_status(db, company_id)
    if st.blocked and not force:
        raise OffLimits(st.reason)
    existing = card(db, company_id)
    if existing:
        if contact_id and not existing["contact_id"]:
            db.conn.execute("UPDATE pipeline SET contact_id = ?, updated_at = ? WHERE id = ?",
                            (contact_id, now(), existing["id"]))
            db.commit()
        return existing["id"]
    pid = db.conn.execute(
        "INSERT INTO pipeline (company_id, stage, contact_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (company_id, stage, contact_id, now(), now())).lastrowid
    log(db, company_id, "stage", f"Added to pipeline: {STAGE_NAMES[stage]}" + (" (off-limits override)" if force and st.blocked else ""))
    db.commit()
    return pid


def set_stage(db: DB, company_id: int, stage: str, reason: str | None = None) -> None:
    if stage not in STAGE_NAMES:
        raise ValueError(stage)
    if not card(db, company_id):
        add_to_pipeline(db, company_id, stage=stage, force=True)
        return
    db.conn.execute("UPDATE pipeline SET stage = ?, lost_reason = ?, updated_at = ? WHERE company_id = ?",
                    (stage, reason if stage in ("lost", "not_now") else None, now(), company_id))
    log(db, company_id, "stage", f"Stage: {STAGE_NAMES[stage]}" + (f" ({reason})" if reason else ""))
    if stage in ("replied", "meeting", "agreement", "job_order", "placement", "lost", "not_now"):
        # Conversation has moved on: stop automated follow-ups at this company.
        db.conn.execute("UPDATE enrollments SET status = 'stopped' WHERE company_id = ? AND status = 'active'",
                        (company_id,))
    db.commit()


def advance_to(db: DB, company_id: int, stage: str) -> None:
    """Move forward only (e.g. first message -> Contacted), never backwards."""
    c = card(db, company_id)
    if not c:
        add_to_pipeline(db, company_id, stage=stage, force=True)
    elif ORDER[stage] > ORDER[c["stage"]] and c["stage"] not in ("lost", "not_now"):
        set_stage(db, company_id, stage)


def update_card(db: DB, company_id: int, **fields) -> None:
    allowed = {k: (v or None) for k, v in fields.items() if k in ("contact_id", "owner", "next_step", "due_date")}
    if not allowed:
        return
    if not card(db, company_id):
        add_to_pipeline(db, company_id, force=True)
    cols = ", ".join(f"{k} = ?" for k in allowed)
    db.conn.execute(f"UPDATE pipeline SET {cols}, updated_at = ? WHERE company_id = ?",
                    (*allowed.values(), now(), company_id))
    if fields.get("next_step"):
        log(db, company_id, "system", f"Next step: {fields['next_step']}" +
            (f" (due {fields['due_date']})" if fields.get("due_date") else ""))
    db.commit()


def add_note(db: DB, company_id: int, text: str, person_id: int | None = None) -> None:
    if text.strip():
        log(db, company_id, "note", text.strip(), person_id)
        db.commit()


def board(db: DB) -> dict[str, list[dict]]:
    rows = db.conn.execute(
        "SELECT pl.*, co.name AS company_name, co.key AS company_key, co.city, p.name AS contact_name, "
        "p.title AS contact_title FROM pipeline pl JOIN companies co ON co.id = pl.company_id "
        "LEFT JOIN people p ON p.id = pl.contact_id ORDER BY pl.due_date IS NULL, pl.due_date, pl.updated_at DESC"
    ).fetchall()
    out: dict[str, list[dict]] = {k: [] for k, _ in STAGES}
    t = today()
    for r in rows:
        d = dict(r)
        d["overdue"] = bool(d["due_date"] and d["due_date"] < t)
        d["offlimits"] = offlimits_status(db, d["company_id"])
        out.setdefault(d["stage"], []).append(d)
    return out


def timeline(db: DB, company_id: int, limit: int = 100) -> list[dict]:
    rows = db.conn.execute(
        "SELECT a.*, p.name AS person_name FROM activities a LEFT JOIN people p ON p.id = a.person_id "
        "WHERE a.company_id = ? ORDER BY a.created_at DESC, a.id DESC LIMIT ?", (company_id, limit)).fetchall()
    return [dict(r) for r in rows]


# --- templates & sequences ------------------------------------------------------------

def ensure_starters(db: DB) -> None:
    messaging.ensure_starters(db)
    if not db.templates("email"):
        for name, subject, body in STARTER_EMAILS:
            db.save_template(name, body, kind="email", subject=subject)
    if not db.conn.execute("SELECT 1 FROM sequences").fetchone():
        name, steps = STARTER_SEQUENCE
        sid = db.conn.execute("INSERT INTO sequences (name) VALUES (?)", (name,)).lastrowid
        by_name = {t["name"]: t["id"] for t in db.templates(None)}
        for i, (day, channel, tname) in enumerate(steps, 1):
            db.conn.execute("INSERT INTO sequence_steps (sequence_id, step_no, day, channel, template_id) "
                            "VALUES (?, ?, ?, ?, ?)", (sid, i, day, channel, by_name.get(tname)))
        db.commit()


def sequences(db: DB) -> list[dict]:
    out = []
    for s in db.conn.execute("SELECT * FROM sequences ORDER BY id").fetchall():
        steps = db.conn.execute(
            "SELECT st.*, t.name AS template_name FROM sequence_steps st LEFT JOIN templates t ON t.id = st.template_id "
            "WHERE st.sequence_id = ? ORDER BY st.step_no", (s["id"],)).fetchall()
        out.append({**dict(s), "steps": [dict(x) for x in steps]})
    return out


def save_sequence(db: DB, name: str, steps: list[tuple[int, str, int | None]], sequence_id: int | None = None) -> int:
    """steps: [(day, channel, template_id)] in order."""
    if sequence_id:
        db.conn.execute("UPDATE sequences SET name = ? WHERE id = ?", (name, sequence_id))
        db.conn.execute("DELETE FROM sequence_steps WHERE sequence_id = ?", (sequence_id,))
    else:
        sequence_id = db.conn.execute("INSERT INTO sequences (name) VALUES (?)", (name,)).lastrowid
    for i, (day, channel, tid) in enumerate(sorted(steps, key=lambda s: s[0]), 1):
        db.conn.execute("INSERT INTO sequence_steps (sequence_id, step_no, day, channel, template_id) "
                        "VALUES (?, ?, ?, ?, ?)", (sequence_id, i, int(day), channel, tid))
    db.commit()
    return sequence_id


# --- enrolment --------------------------------------------------------------------

def _step(db: DB, sequence_id: int, step_no: int):
    return db.conn.execute("SELECT * FROM sequence_steps WHERE sequence_id = ? AND step_no = ?",
                           (sequence_id, step_no)).fetchone()


def enroll(db: DB, person_id: int, sequence_id: int, force: bool = False) -> int:
    p = db.conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
    if not p:
        raise ValueError("No such person")
    st = offlimits_status(db, p["company_id"])
    if st.blocked and not force:
        raise OffLimits(st.reason)
    has_email = p["email"] and not db.is_suppressed(p["email"])
    has_phone = p["phone"] and not db.is_suppressed("+" + messaging.digits(p["phone"]))
    if not (has_email or has_phone):
        raise ValueError(f"{p['name']} has no email or phone to follow up on")
    existing = db.conn.execute("SELECT * FROM enrollments WHERE person_id = ? AND sequence_id = ?",
                               (person_id, sequence_id)).fetchone()
    if existing:
        if existing["status"] in ("active", "replied", "unsubscribed"):
            return existing["id"]
        db.conn.execute("UPDATE enrollments SET status = 'active', step_no = 1, started_at = ?, next_due = ? "
                        "WHERE id = ?", (today(), today(), existing["id"]))
        eid = existing["id"]
    else:
        eid = db.conn.execute(
            "INSERT INTO enrollments (person_id, company_id, sequence_id, started_at, next_due) VALUES (?, ?, ?, ?, ?)",
            (person_id, p["company_id"], sequence_id, today(), today())).lastrowid
    add_to_pipeline(db, p["company_id"], contact_id=person_id, force=True)
    seq = db.conn.execute("SELECT name FROM sequences WHERE id = ?", (sequence_id,)).fetchone()
    log(db, p["company_id"], "system", f"Started follow-ups: {seq['name'] if seq else sequence_id}", person_id)
    db.commit()
    return eid


def stop(db: DB, enrollment_id: int, status: str = "stopped") -> None:
    e = db.conn.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    if e:
        db.conn.execute("UPDATE enrollments SET status = ? WHERE id = ?", (status, enrollment_id))
        log(db, e["company_id"], "system", f"Follow-ups {status}", e["person_id"])
        db.commit()


def _advance(db: DB, e) -> None:
    nxt = _step(db, e["sequence_id"], e["step_no"] + 1)
    if not nxt:
        db.conn.execute("UPDATE enrollments SET status = 'finished', step_no = step_no + 1, next_due = NULL "
                        "WHERE id = ?", (e["id"],))
        return
    start = date.fromisoformat(e["started_at"][:10])
    due = max(start + timedelta(days=nxt["day"]), date.today() + timedelta(days=1))
    db.conn.execute("UPDATE enrollments SET step_no = ?, next_due = ? WHERE id = ?",
                    (nxt["step_no"], due.isoformat(), e["id"]))


def skip_step(db: DB, enrollment_id: int) -> None:
    e = db.conn.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    if e:
        _advance(db, e)
        db.commit()


# --- composing ---------------------------------------------------------------------

def footer(db: DB) -> str:
    me = db.get_setting("my_name") or os.environ.get("MY_NAME", "")
    co = db.get_setting("my_company") or os.environ.get("MY_COMPANY", "")
    addr = db.get_setting("my_address") or os.environ.get("MY_ADDRESS", "")
    lines = [x for x in [", ".join(x for x in [me, co] if x), addr] if x]
    return "\n\n--\n" + "\n".join(lines + ['Not the right person, or rather not hear from me? Reply "unsubscribe".'])


def compose(db: DB, template, person_id: int) -> dict:
    vals = messaging.person_fields(db, person_id)
    body, missing = messaging.render(template["body"], vals)
    subject, m2 = messaging.render(template["subject"] or "", vals) if template["subject"] else ("", [])
    if template["kind"] == "email":
        body += footer(db)
    return {"subject": subject, "body": body, "missing": sorted(set(missing) | set(m2))}


def due_items(db: DB, on: str | None = None) -> list[dict]:
    """Everything to do today: follow-up steps and pipeline next steps."""
    on = on or today()
    items: list[dict] = []
    rows = db.conn.execute(
        "SELECT e.*, p.name, p.email, p.phone, p.title, co.name AS company_name FROM enrollments e "
        "JOIN people p ON p.id = e.person_id LEFT JOIN companies co ON co.id = e.company_id "
        "WHERE e.status = 'active' AND e.next_due <= ? ORDER BY e.next_due, e.id", (on,)).fetchall()
    for e in rows:
        st = offlimits_status(db, e["company_id"])
        if st.blocked:
            stop(db, e["id"], "stopped")
            log(db, e["company_id"], "system", f"Follow-ups stopped: {st.reason}", e["person_id"])
            db.commit()
            continue
        step = _step(db, e["sequence_id"], e["step_no"])
        if not step:
            _advance(db, e)
            db.commit()
            continue
        to = e["email"] if step["channel"] == "email" else e["phone"]
        if to and step["channel"] == "email" and db.is_suppressed(to):
            to = None
        if to and step["channel"] == "text" and db.is_suppressed("+" + messaging.digits(to)):
            to = None
        tpl = db.template(step["template_id"]) if step["template_id"] else None
        if not to or not tpl:
            _advance(db, e)  # nothing to send this way: move on to the next step
            db.commit()
            continue
        msg = compose(db, tpl, e["person_id"])
        items.append({"kind": "step", "enrollment_id": e["id"], "channel": step["channel"], "to": to,
                      "person_id": e["person_id"], "company_id": e["company_id"], "name": e["name"],
                      "title": e["title"], "company_name": e["company_name"], "step_no": e["step_no"],
                      "due": e["next_due"], "template_id": tpl["id"], **msg})
    for c in db.conn.execute(
            "SELECT pl.*, co.name AS company_name FROM pipeline pl JOIN companies co ON co.id = pl.company_id "
            "WHERE pl.next_step IS NOT NULL AND pl.due_date IS NOT NULL AND pl.due_date <= ? "
            "AND pl.stage NOT IN ('lost') ORDER BY pl.due_date", (on,)).fetchall():
        items.append({"kind": "task", "company_id": c["company_id"], "company_name": c["company_name"],
                      "next_step": c["next_step"], "due": c["due_date"]})
    return items


def task_done(db: DB, company_id: int) -> None:
    c = card(db, company_id)
    if c and c["next_step"]:
        log(db, company_id, "system", f"Done: {c['next_step']}")
        db.conn.execute("UPDATE pipeline SET next_step = NULL, due_date = NULL, updated_at = ? WHERE company_id = ?",
                        (now(), company_id))
        db.commit()


# --- sending ------------------------------------------------------------------------

def mailto_link(to: str, subject: str, body: str) -> str:
    return f"mailto:{to}?subject={quote(subject)}&body={quote(body)}"


def smtp_configured() -> bool:
    return all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"))


def imap_configured() -> bool:
    return bool(os.environ.get("IMAP_HOST") and (os.environ.get("IMAP_USER") or os.environ.get("SMTP_USER"))
                and (os.environ.get("IMAP_PASSWORD") or os.environ.get("SMTP_PASSWORD")))


def test_connection() -> list[str]:
    """Log in to SMTP (and IMAP if set) without sending anything. Returns problems found."""
    problems = []
    try:
        host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT") or 587)
        server = smtplib.SMTP_SSL(host, port, timeout=20) if port == 465 else smtplib.SMTP(host, port, timeout=20)
        with server:
            if port != 465:
                server.starttls()
            server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
    except smtplib.SMTPAuthenticationError:
        problems.append("Sending: Google rejected the login. Use an app password (not your normal password), "
                        "and check the address.")
    except Exception as e:  # noqa: BLE001
        problems.append(f"Sending: couldn't connect ({e}).")
    if imap_configured():
        try:
            with imaplib.IMAP4_SSL(os.environ["IMAP_HOST"], int(os.environ.get("IMAP_PORT") or 993)) as box:
                box.login(os.environ.get("IMAP_USER") or os.environ["SMTP_USER"],
                          os.environ.get("IMAP_PASSWORD") or os.environ["SMTP_PASSWORD"])
        except imaplib.IMAP4.error:
            problems.append("Reading replies: login rejected. In Gmail settings, make sure IMAP is enabled.")
        except Exception as e:  # noqa: BLE001
            problems.append(f"Reading replies: couldn't connect ({e}).")
    return problems


def daily_limit() -> int:
    try:
        return int(os.environ.get("EMAIL_DAILY_LIMIT") or 40)
    except ValueError:
        return 40


def sent_today(db: DB) -> int:
    return db.conn.execute("SELECT COUNT(*) FROM emails WHERE method = 'smtp' AND sent_at >= ?",
                           (today(),)).fetchone()[0]


def send_smtp(db: DB, to: str, subject: str, body: str) -> str:
    """Send from your own mailbox. Returns the Message-ID."""
    if sent_today(db) >= daily_limit():
        raise RuntimeError(f"Daily email limit reached ({daily_limit()}); the rest stay due for tomorrow.")
    msg = EmailMessage()
    sender = os.environ.get("SMTP_FROM") or os.environ["SMTP_USER"]
    my_name = db.get_setting("my_name") or os.environ.get("MY_NAME", "")
    msg["From"] = email.utils.formataddr((my_name, sender)) if my_name else sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=sender.split("@")[-1])
    msg["List-Unsubscribe"] = f"<mailto:{sender}?subject=unsubscribe>"
    msg.set_content(body)
    host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT") or 587)
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls()
    with server:
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        server.send_message(msg)
    return msg["Message-ID"]


def record_email(db: DB, to: str, subject: str, body: str, method: str, person_id: int | None = None,
                 company_id: int | None = None, enrollment_id: int | None = None,
                 message_id: str | None = None, step_no: int | None = None) -> None:
    db.conn.execute(
        "INSERT INTO emails (person_id, company_id, enrollment_id, step_no, to_addr, subject, body, method, "
        "message_id, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (person_id, company_id, enrollment_id, step_no, to.lower(), subject, body, method, message_id, now()))
    log(db, company_id, "email", f"{subject}\n\n{body}", person_id)
    if company_id:
        advance_to(db, company_id, "contacted")
    db.commit()


def complete_step(db: DB, enrollment_id: int, method: str, subject: str = "", body: str = "",
                  to: str | None = None) -> None:
    """Log a sent follow-up step and schedule the next one."""
    e = db.conn.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    if not e or e["status"] != "active":
        return
    step = _step(db, e["sequence_id"], e["step_no"])
    if step and step["channel"] == "email":
        record_email(db, to, subject, body, method, e["person_id"], e["company_id"], enrollment_id,
                     step_no=e["step_no"])
    else:
        messaging.log_message(db, to, body, "sms" if method != "phone" else "phone", e["person_id"],
                              e["company_id"], step["template_id"] if step else None)
        log(db, e["company_id"], "text", body, e["person_id"])
        advance_to(db, e["company_id"], "contacted")
    _advance(db, e)
    db.commit()


def send_due_emails(db: DB, limit: int | None = None, pause: float = 0.0, progress=None) -> dict:
    """Send every due email step through your mailbox (up to the daily limit)."""
    import time
    say = progress or (lambda m: None)
    sent = failed = 0
    for it in due_items(db):
        if it["kind"] != "step" or it["channel"] != "email":
            continue
        if (limit is not None and sent >= limit) or sent_today(db) >= daily_limit():
            break
        try:
            mid = send_smtp(db, it["to"], it["subject"], it["body"])
        except Exception as ex:  # noqa: BLE001 - report and carry on
            failed += 1
            say(f"✗ {it['to']}: {ex}")
            continue
        complete_step(db, it["enrollment_id"], "smtp", it["subject"], it["body"], it["to"])
        db.conn.execute("UPDATE emails SET message_id = ? WHERE id = (SELECT MAX(id) FROM emails)", (mid,))
        db.commit()
        sent += 1
        say(f"✓ {it['name']} <{it['to']}>")
        if pause:
            time.sleep(pause)
    return {"sent": sent, "failed": failed}


# --- reading replies and bounces ------------------------------------------------------

def _text_of(msg) -> str:
    parts = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() in ("text/plain", "message/delivery-status", "text/rfc822-headers"):
            try:
                parts.append(part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace"))
            except Exception:  # noqa: BLE001
                continue
    return "\n".join(parts)


def _is_auto_reply(msg) -> bool:
    auto = (msg.get("Auto-Submitted") or "no").lower()
    return (auto != "no" or bool(msg.get("X-Autoreply")) or bool(msg.get("X-Autorespond"))
            or bool(re.search(r"automatic reply|auto[- ]?reply|out of (the )?office|away from", msg.get("Subject", ""), re.I)))


def process_message(db: DB, raw: bytes) -> str | None:
    """Handle one inbox message. Returns 'reply', 'unsubscribe', 'bounce' or None."""
    from .emailcheck import mark_bad
    msg = email.message_from_bytes(raw)
    mid = (msg.get("Message-ID") or "").strip()
    if mid and db.conn.execute("SELECT 1 FROM app_settings WHERE key = ?", ("seen:" + mid,)).fetchone():
        return None
    sender = email.utils.parseaddr(msg.get("From", ""))[1].lower()
    subject = msg.get("Subject", "")
    text = _text_of(msg)
    result = None
    emailed = {r[0] for r in db.conn.execute("SELECT DISTINCT to_addr FROM emails")}

    if BOUNCE_FROM.search(msg.get("From", "")) or BOUNCE_SUBJECT.search(subject):
        for addr in sorted(a for a in emailed if a in text.lower()):
            last = db.conn.execute("SELECT * FROM emails WHERE to_addr = ? ORDER BY id DESC LIMIT 1", (addr,)).fetchone()
            log(db, last["company_id"], "bounce", f"Bounced: {addr}", last["person_id"])
            results = mark_bad(db, addr, "bounced")
            if last["enrollment_id"]:
                e = db.conn.execute("SELECT * FROM enrollments WHERE id = ?", (last["enrollment_id"],)).fetchone()
                new_addr = next((n for _, n in results if n), None)
                if e and e["status"] in ("active", "finished") and new_addr and last["step_no"]:
                    # Resend the step that bounced, to the next guess.
                    db.conn.execute("UPDATE enrollments SET status = 'active', step_no = ?, next_due = ? WHERE id = ?",
                                    (last["step_no"], today(), e["id"]))
            result = "bounce"
    elif sender and sender in emailed and _is_auto_reply(msg):
        p = db.conn.execute("SELECT * FROM people WHERE lower(email) = ?", (sender,)).fetchone()
        if p:
            log(db, p["company_id"], "system", f"Auto-reply: {subject}", p["id"])
        result = None
    elif sender and sender in emailed:
        people = db.conn.execute("SELECT * FROM people WHERE lower(email) = ?", (sender,)).fetchall()
        unsub = bool(UNSUBSCRIBE.search(subject + "\n" + text[:2000]))
        for p in people:
            if unsub:
                db.suppress(sender, "unsubscribed by reply")
                db.conn.execute("UPDATE enrollments SET status = 'unsubscribed' WHERE person_id = ? AND status = 'active'",
                                (p["id"],))
                log(db, p["company_id"], "reply", f"Unsubscribed: {subject}", p["id"])
            else:
                db.conn.execute("UPDATE enrollments SET status = 'replied' WHERE person_id = ? AND status = 'active'",
                                (p["id"],))
                snippet = re.split(r"\n\s*(On .+wrote:|-----Original Message|From:)", text)[0].strip()[:600]
                log(db, p["company_id"], "reply", f"{subject}\n\n{snippet}", p["id"])
                advance_to(db, p["company_id"], "replied")
        result = "unsubscribe" if unsub else ("reply" if people else None)
    if mid:
        db.conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)", ("seen:" + mid, now()))
    db.commit()
    return result


def check_inbox(db: DB, days: int = 30) -> dict:
    """Read recent inbox messages for replies and bounces (your own mailbox, read-only)."""
    host = os.environ["IMAP_HOST"]
    user = os.environ.get("IMAP_USER") or os.environ["SMTP_USER"]
    password = os.environ.get("IMAP_PASSWORD") or os.environ["SMTP_PASSWORD"]
    since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
    counts = {"reply": 0, "unsubscribe": 0, "bounce": 0, "checked": 0}
    with imaplib.IMAP4_SSL(host, int(os.environ.get("IMAP_PORT") or 993)) as box:
        box.login(user, password)
        box.select("INBOX", readonly=True)
        _, data = box.search(None, "SINCE", since)
        for num in (data[0].split() if data and data[0] else [])[-500:]:
            _, msg_data = box.fetch(num, "(BODY.PEEK[])")
            raw = next((x[1] for x in msg_data if isinstance(x, tuple)), None)
            if not raw:
                continue
            counts["checked"] += 1
            r = process_message(db, raw)
            if r:
                counts[r] += 1
    db.set_setting("inbox_checked_at", datetime.now().isoformat(timespec="seconds"))
    return counts
