"""Email guess feedback loop.

  wrong / bounced  -> mark_bad():  remember the address and its format, try the next guess
  right / replied  -> mark_good(): lock in the company's format, re-guess colleagues with it
  after MAX_ATTEMPTS wrong guesses (or no guesses left) the person is marked "failed"

Everything runs on your computer: no outside verification service is used.
"""

from __future__ import annotations

import json

from .db import DB, now
from .exclusions import is_excluded_email
from .extract import find_emails
from .patterns import candidates

MAX_ATTEMPTS = 10


def _base(pattern: str | None) -> str | None:
    return pattern.replace(" (nickname)", "") if pattern else None


def _tried(row) -> list[str]:
    try:
        return json.loads(row["tried_emails"] or "[]")
    except (TypeError, ValueError):
        return []


def _person(db: DB, person_id: int):
    return db.conn.execute(
        "SELECT p.*, co.domain, co.email_pattern AS company_pattern FROM people p "
        "JOIN companies co ON co.id = p.company_id WHERE p.id = ?", (person_id,)).fetchone()


def _feedback(db: DB, company_id: int, pattern: str | None, good: int = 0, bad: int = 0) -> None:
    if not pattern:
        return
    db.conn.execute(
        "INSERT INTO pattern_feedback (company_id, pattern, good, bad) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(company_id, pattern) DO UPDATE SET good = good + excluded.good, bad = bad + excluded.bad",
        (company_id, pattern, good, bad))


def ranked_patterns(db: DB, company_id: int) -> tuple[list[str], set[str]]:
    """(preferred, avoid): formats confirmed here first, then the learned one;
    formats that only ever bounced here go last."""
    rows = db.conn.execute("SELECT pattern, good, bad FROM pattern_feedback WHERE company_id = ?",
                           (company_id,)).fetchall()
    confirmed = [r["pattern"] for r in sorted(rows, key=lambda r: -r["good"]) if r["good"] > 0]
    avoid = {r["pattern"] for r in rows if r["bad"] > 0 and r["good"] == 0}
    learned = db.company_row(company_id)["email_pattern"]
    preferred = confirmed + ([learned] if learned and learned not in avoid else [])
    return list(dict.fromkeys(preferred)), avoid


def _set_guess(db: DB, row, email: str | None, pattern: str | None, status: str) -> None:
    # Keep the contacts table in step: drop the old guess, add the new one.
    if row["email"] and row["email_status"] in ("guessed", "catch_all"):
        db.conn.execute("DELETE FROM contacts WHERE value = ? AND kind = 'guessed'", (row["email"],))
    db.conn.execute("UPDATE people SET email = ?, email_status = ?, email_pattern = ? WHERE id = ?",
                    (email, status, pattern, row["id"]))
    if email:
        db.add_contact(row["company_id"], "email", email, "guessed", name=row["name"], title=row["title"])


def next_guess(db: DB, person_id: int) -> str | None:
    """Give the person the best untried guess. Returns it, or None when out of tries."""
    row = _person(db, person_id)
    if not row or not row["domain"]:
        return None
    tried = set(_tried(row))
    if row["email_attempts"] >= MAX_ATTEMPTS:
        _set_guess(db, row, None, None, "failed")
        db.commit()
        return None
    preferred, avoid = ranked_patterns(db, row["company_id"])
    for email, pattern in candidates(row["name"], row["domain"], preferred, avoid):
        if email in tried or db.is_bad_email(email) or db.is_suppressed(email) or is_excluded_email(email):
            continue
        if db.conn.execute("SELECT 1 FROM people WHERE email = ? AND id != ?", (email, person_id)).fetchone():
            continue  # a colleague already owns that address
        _set_guess(db, row, email, pattern, "guessed")
        db.commit()
        return email
    _set_guess(db, row, None, None, "failed")
    db.commit()
    return None


def mark_bad(db: DB, email: str, reason: str = "bounced") -> list[tuple[str, str | None]]:
    """An address doesn't work. Returns [(person name, next guess or None)]."""
    email = email.strip().lower()
    db.conn.execute("INSERT OR REPLACE INTO bad_emails (value, reason, added_at) VALUES (?, ?, ?)",
                    (email, reason, now()))
    db.conn.execute("DELETE FROM contacts WHERE type = 'email' AND value = ?", (email,))
    out = []
    for row in db.conn.execute("SELECT * FROM people WHERE email = ?", (email,)).fetchall():
        tried = _tried(row) + [email]
        _feedback(db, row["company_id"], _base(row["email_pattern"]), bad=1)
        db.conn.execute("UPDATE people SET email = NULL, email_attempts = email_attempts + 1, "
                        "tried_emails = ? WHERE id = ?", (json.dumps(tried), row["id"]))
        db.commit()
        out.append((row["name"], next_guess(db, row["id"])))
    db.commit()
    return out


def mark_good(db: DB, email: str) -> int:
    """An address works. Locks in the format and re-guesses colleagues. Returns
    how many colleagues' guesses changed."""
    email = email.strip().lower()
    row = db.conn.execute("SELECT * FROM people WHERE email = ?", (email,)).fetchone()
    db.conn.execute("DELETE FROM bad_emails WHERE value = ?", (email,))
    if not row:
        return 0
    pattern = _base(row["email_pattern"])
    db.conn.execute("UPDATE people SET email_status = 'confirmed' WHERE id = ?", (row["id"],))
    db.conn.execute("UPDATE contacts SET kind = 'person' WHERE value = ?", (email,))
    changed = 0
    if pattern:
        _feedback(db, row["company_id"], pattern, good=1)
        db.update_company(row["company_id"], email_pattern=pattern, pattern_confidence=1.0)
        db.commit()
        # Colleagues still on an unconfirmed guess switch to the format that worked.
        for p in db.conn.execute("SELECT id, email FROM people WHERE company_id = ? AND id != ? "
                                 "AND email_status IN ('guessed', 'failed')",
                                 (row["company_id"], row["id"])).fetchall():
            if next_guess(db, p["id"]) != p["email"]:
                changed += 1
    db.commit()
    return changed


def import_bounces(db: DB, text: str) -> list[tuple[str, str, str | None]]:
    """Paste bounce notifications, a CSV, or a list of addresses. Only addresses
    we have saved are acted on. Returns [(bad email, person, next guess)]."""
    out = []
    known = {r[0] for r in db.conn.execute("SELECT email FROM people WHERE email IS NOT NULL")}
    for email in sorted(find_emails(text) & known):
        for name, nxt in mark_bad(db, email, "bounced"):
            out.append((email, name, nxt))
    return out


def guess_all(db: DB, company_id: int) -> int:
    """First guess for everyone at a company who has no email yet."""
    n = 0
    for p in db.conn.execute("SELECT id FROM people WHERE company_id = ? AND email IS NULL "
                             "AND (email_status IS NULL OR email_status != 'failed')", (company_id,)).fetchall():
        if next_guess(db, p["id"]):
            n += 1
    return n


__all__ = ["MAX_ATTEMPTS", "mark_bad", "mark_good", "next_guess", "import_bounces", "guess_all"]
