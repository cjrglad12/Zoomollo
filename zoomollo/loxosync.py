"""Log what you do in Zoomollo into Loxo, so nothing is typed twice.

Every call outcome, email, text and pitch you log goes into a small outbox
(table loxo_outbox). A background job sends it to Loxo as an activity on the
right person, about 10 minutes later — long enough for Undo on Today to take it
back before it leaves.

Finding the person in Loxo: their saved Loxo id, else a search by email (only
emails found on a website or confirmed, never guesses), else name + company.
If they aren't in Loxo yet, they're created as a Contact (setting
"loxo_create_contacts", on by default) with only verified details.
Calls to a main line with no named person can't be logged in Loxo (an activity
needs a person) and are marked "skipped" with the reason.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import requests

from .db import DB, now

log = logging.getLogger(__name__)

DELAY_MINUTES = 10          # matches Today's Undo window
CONTACT_TYPE = "Contact"    # Loxo person type for prospects / hiring managers
MAX_TRIES = 5
NOTE_PREFIX = "Zoomollo · "


def loxo_type_for(kind: str, body: str) -> tuple[str | None, str]:
    """(Loxo activity type name, notes) for one Zoomollo activity, or (None, '') to not sync it."""
    b = (body or "").strip()
    low = b.lower()
    if kind == "call":
        if low.startswith("call: left voicemail"):
            return "Left VoiceMail", b
        if low.startswith("meeting booked"):
            return "Meeting Scheduled", b
        if low.startswith("not interested"):
            return "*Note", b
        return "Phone Call", b            # connected / no answer: the note says which
    if kind == "email":
        return "Sent Email", b[:2000]
    if kind == "text":
        return "Sent Text Message", b[:2000]
    return None, ""


def enqueue(db: DB, activity_id: int | None, company_id: int | None, person_id: int | None,
            kind: str, body: str) -> None:
    """Called whenever Zoomollo logs an activity (crm.log)."""
    type_name, notes = loxo_type_for(kind, body)
    if not type_name or not sync_enabled(db):
        return
    db.conn.execute(
        "INSERT INTO loxo_outbox (activity_id, company_id, person_id, activity_type, notes, happened_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", (activity_id, company_id, person_id, type_name, notes, now(), now()))


def enqueue_pitch(db: DB, candidate_id: int, company_id: int | None, notes: str) -> None:
    """A pitch is also logged on the candidate's own Loxo record as 'Pitched'."""
    if not sync_enabled(db):
        return
    db.conn.execute(
        "INSERT INTO loxo_outbox (company_id, candidate_id, activity_type, notes, happened_at, updated_at) "
        "VALUES (?, ?, 'Pitched', ?, ?, ?)", (company_id, candidate_id, notes, now(), now()))
    db.commit()


def forget_activities(db: DB, company_id: int, after_activity_id: int) -> int:
    """Undo on Today: drop anything not sent yet for activities that were just undone."""
    n = db.conn.execute("DELETE FROM loxo_outbox WHERE status = 'pending' AND company_id = ? AND "
                        "(activity_id > ? OR (activity_id IS NULL AND happened_at >= ?))",
                        (company_id, after_activity_id,
                         (datetime.now(timezone.utc) - timedelta(minutes=DELAY_MINUTES)).isoformat())).rowcount
    db.commit()
    return n


def sync_enabled(db: DB) -> bool:
    from .loxo import configured
    return configured() and (db.get_setting("loxo_activity_sync") or "1") == "1"


def create_contacts(db: DB) -> bool:
    return (db.get_setting("loxo_create_contacts") or "1") == "1"


# --- talking to Loxo --------------------------------------------------------------------

def _people(resp) -> list[dict]:
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in ("people", "results", "data"):
            if isinstance(resp.get(k), list):
                return resp[k]
    return []


def _norm(s: str | None) -> str:
    from .orgmap import norm_company
    return norm_company(s or "")


def _quote(s: str) -> str:
    return '"' + re.sub(r'(["\\])', r"\\\1", s) + '"'


def _emails(p: dict) -> set[str]:
    out = {(e.get("value") if isinstance(e, dict) else e or "").strip().lower() for e in (p.get("emails") or [])}
    if p.get("email"):
        out.add(str(p["email"]).strip().lower())
    return {e for e in out if e}


def _companies(p: dict) -> set[str]:
    names = {p.get("current_company"), p.get("company")}
    for jp in p.get("job_profiles") or []:
        if isinstance(jp, dict) and isinstance(jp.get("company"), dict):
            names.add(jp["company"].get("name"))
    return {_norm(n) for n in names if isinstance(n, str) and _norm(n)}


class Sync:
    def __init__(self, db: DB, client=None):
        from .loxo import LoxoClient
        self.db = db
        self.client = client or LoxoClient()
        self._types: dict[str, int] | None = None
        self._contact_type: int | None = None

    def type_id(self, name: str) -> int | None:
        if self._types is None:
            d = self.client.get("activity_types")
            items = d if isinstance(d, list) else next((v for v in d.values() if isinstance(v, list)), [])
            self._types = {str(i.get("name")).strip().lower(): i.get("id") for i in items if isinstance(i, dict)}
        return self._types.get(name.strip().lower())

    def contact_type(self) -> int | None:
        if self._contact_type is None:
            d = self.client.get("person_types")
            items = d if isinstance(d, list) else next((v for v in d.values() if isinstance(v, list)), [])
            self._contact_type = next((i.get("id") for i in items if str(i.get("name")).lower() == "contact"), 0)
        return self._contact_type or None

    def post(self, path: str, payload: dict) -> dict:
        from .loxo import request_with_backoff
        r = request_with_backoff(
            lambda: self.client.session.post(f"{self.client.base}/{path}", json=payload, timeout=30))
        if r.status_code in (400, 422):
            raise ValueError(f"Loxo rejected it: {r.text[:300]}")
        r.raise_for_status()
        return r.json() if r.content.strip() else {}

    def loxo_company_id(self, company_id: int | None) -> int | None:
        if not company_id:
            return None
        co = self.db.company_row(company_id)
        if not co:
            return None
        row = None
        if co["domain"]:
            row = self.db.conn.execute("SELECT loxo_id FROM loxo_companies WHERE domain = ? LIMIT 1",
                                       (co["domain"],)).fetchone()
        if not row and co["name"]:
            row = self.db.conn.execute("SELECT loxo_id FROM loxo_companies WHERE name_norm = ? LIMIT 1",
                                       (_norm(co["name"]),)).fetchone()
        return row[0] if row else None

    def find_or_create(self, person_id: int) -> tuple[int | None, str]:
        """(Loxo person id, how it was found) for a Zoomollo contact."""
        p = self.db.conn.execute("SELECT p.*, co.name AS company_name FROM people p JOIN companies co "
                                 "ON co.id = p.company_id WHERE p.id = ?", (person_id,)).fetchone()
        if not p:
            return None, "contact no longer in Zoomollo"
        if p["loxo_person_id"]:
            return p["loxo_person_id"], "linked"
        email = (p["email"] or "").lower() if p["email_status"] in ("found", "confirmed") else ""
        company = _norm(p["company_name"])
        found = None
        if email:
            for hit in _people(self.client.get("people", query=_quote(email), per_page=10)):
                if email in _emails(hit):
                    found = hit["id"]
                    break
        if not found:
            for hit in _people(self.client.get("people", query=_quote(p["name"]), per_page=25)):
                if (hit.get("name") or "").strip().lower() != p["name"].strip().lower():
                    continue
                cos = _companies(hit)
                if not (cos and company in cos):
                    full = self.client.get(f"people/{int(hit['id'])}")
                    cos = _companies(full if isinstance(full, dict) else {})
                if company and company in cos:
                    found = hit["id"]
                    break
        how = "matched"
        if not found:
            if not create_contacts(self.db):
                return None, "not in Loxo (creating contacts is turned off)"
            person = {"name": p["name"]}
            for k, v in (("email", email), ("phone", p["phone"]), ("title", p["title"]),
                         ("company", p["company_name"]), ("linkedin_url", p["linkedin_url"])):
                if v:
                    person[k] = v
            ct = self.contact_type()
            if ct:
                person["person_type_id"] = [ct]
            resp = self.post("people", {"person": person})
            created = resp.get("person", resp) if isinstance(resp, dict) else {}
            found = created.get("id") if isinstance(created, dict) else None
            if not found:
                return None, "Loxo didn't return the new contact"
            how = "created"
        self.db.conn.execute("UPDATE people SET loxo_person_id = ?, pushed_to_loxo = 1 WHERE id = ?", (found, person_id))
        self.db.commit()
        return found, how

    def send(self, row) -> tuple[str, str]:
        """(status, detail) for one outbox row."""
        if row["candidate_id"]:
            c = self.db.conn.execute("SELECT loxo_id FROM candidates WHERE id = ?", (row["candidate_id"],)).fetchone()
            if not c or not c["loxo_id"]:
                return "skipped", "candidate isn't from Loxo"
            loxo_pid, how = int(c["loxo_id"]), "candidate"
        elif row["person_id"]:
            loxo_pid, how = self.find_or_create(row["person_id"])
            if not loxo_pid:
                return "skipped", how
        else:
            return "skipped", "no named contact (main-line call); Loxo activities need a person"
        tid = self.type_id(row["activity_type"])
        if not tid:
            return "failed", f"Loxo has no activity type called {row['activity_type']!r}"
        ev = {"activity_type_id": int(tid), "person_id": int(loxo_pid),
              "notes": NOTE_PREFIX + (row["notes"] or row["activity_type"]), "created_at": row["happened_at"]}
        cid = self.loxo_company_id(row["company_id"])
        if cid:
            ev["company_id"] = int(cid)
        resp = self.post("person_events", {"person_event": ev})
        e = resp.get("person_event", resp) if isinstance(resp, dict) else {}
        return "sent", f"Loxo event {e.get('id', '?')} ({how})"


def flush(db: DB, limit: int = 50, client=None, delay_minutes: int = DELAY_MINUTES, progress=None) -> dict:
    """Send waiting activities to Loxo. Safe to call often; does nothing when there's nothing due."""
    say = progress or (lambda m: None)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=delay_minutes)).isoformat()
    rows = db.conn.execute("SELECT * FROM loxo_outbox WHERE status IN ('pending', 'retry') AND happened_at <= ? "
                           "ORDER BY id LIMIT ?", (cutoff, limit)).fetchall()
    out = {"sent": 0, "skipped": 0, "failed": 0}
    if not rows:
        return out
    try:
        s = Sync(db, client)
    except RuntimeError as e:  # Loxo not set up
        say(f"✗ Loxo sync paused: {e}")
        return out
    for r in rows:
        try:
            status, detail = s.send(r)
        except (requests.RequestException, ValueError) as e:
            tries = r["tries"] + 1
            status = "failed" if tries >= MAX_TRIES or isinstance(e, ValueError) else "retry"
            detail = str(e)[:300]
            db.conn.execute("UPDATE loxo_outbox SET status = ?, detail = ?, tries = ?, updated_at = ? WHERE id = ?",
                            (status, detail, tries, now(), r["id"]))
            db.commit()
            if status == "failed":
                out["failed"] += 1
                say(f"✗ Loxo: couldn't log {r['activity_type']}: {detail[:120]}")
            continue
        db.conn.execute("UPDATE loxo_outbox SET status = ?, detail = ?, tries = tries + 1, updated_at = ? WHERE id = ?",
                        (status, detail, now(), r["id"]))
        db.commit()
        out[status] += 1
    if out["sent"] or out["failed"]:
        say(f"✓ Loxo: {out['sent']} activities logged" + (f", {out['failed']} failed" if out["failed"] else "")
            + (f", {out['skipped']} skipped" if out["skipped"] else ""))
    return out


def status(db: DB) -> dict:
    """Counts for the status line, plus the latest problems."""
    counts = dict(db.conn.execute("SELECT status, COUNT(*) FROM loxo_outbox GROUP BY status").fetchall())
    problems = [dict(r) for r in db.conn.execute(
        "SELECT o.*, co.name AS company_name, p.name AS person_name FROM loxo_outbox o "
        "LEFT JOIN companies co ON co.id = o.company_id LEFT JOIN people p ON p.id = o.person_id "
        "WHERE o.status IN ('failed', 'skipped') ORDER BY o.id DESC LIMIT 10")]
    return {"sent": counts.get("sent", 0), "waiting": counts.get("pending", 0) + counts.get("retry", 0),
            "skipped": counts.get("skipped", 0), "failed": counts.get("failed", 0), "problems": problems,
            "enabled": sync_enabled(db), "create_contacts": create_contacts(db)}


def retry_failed(db: DB) -> int:
    n = db.conn.execute("UPDATE loxo_outbox SET status = 'pending', tries = 0, updated_at = ? WHERE status = 'failed'",
                        (now(),)).rowcount
    db.commit()
    return n
