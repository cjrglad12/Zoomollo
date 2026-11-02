"""SQLite storage. One file, no server."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,           -- domain (acme.com) or board ref (greenhouse:acme)
    domain TEXT,
    name TEXT,
    email_pattern TEXT,
    pattern_confidence REAL,
    last_crawled TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    company_id INTEGER REFERENCES companies(id),
    type TEXT NOT NULL CHECK (type IN ('email', 'phone')),
    value TEXT NOT NULL,
    name TEXT,
    title TEXT,
    kind TEXT NOT NULL,                 -- person | role | company | guessed
    source_url TEXT,
    pushed_to_loxo INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    UNIQUE (type, value)
);
CREATE TABLE IF NOT EXISTS socials (
    id INTEGER PRIMARY KEY,
    company_id INTEGER REFERENCES companies(id),
    kind TEXT,
    url TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    company_id INTEGER REFERENCES companies(id),
    board TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT,
    location TEXT,
    url TEXT,
    posted_at TEXT,
    first_seen TEXT NOT NULL,
    UNIQUE (board, external_id)
);
CREATE TABLE IF NOT EXISTS suppression (
    value TEXT PRIMARY KEY,             -- email, phone (E.164) or @domain.com
    reason TEXT,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    title TEXT,
    department TEXT,
    seniority TEXT,
    email TEXT,
    email_status TEXT,                  -- found | guessed
    phone TEXT,
    linkedin_url TEXT,
    source TEXT,                        -- website | search
    source_url TEXT,
    hiring_for TEXT,                    -- open roles this person likely owns
    first_seen TEXT NOT NULL,
    UNIQUE (company_id, name_key)
);
CREATE INDEX IF NOT EXISTS people_company ON people(company_id);
CREATE TABLE IF NOT EXISTS bad_emails (     -- addresses known not to exist (bounced / verifier said invalid)
    value TEXT PRIMARY KEY,
    reason TEXT,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pattern_feedback (  -- which email formats worked / bounced per company
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    pattern TEXT NOT NULL,
    good INTEGER NOT NULL DEFAULT 0,
    bad INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (company_id, pattern)
);
CREATE TABLE IF NOT EXISTS job_contacts (      -- contacts cross-referenced to an open role
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    person_id INTEGER REFERENCES people(id) ON DELETE CASCADE,
    value TEXT,                                -- email / phone when there is no person
    reason TEXT NOT NULL,
    rank INTEGER NOT NULL,
    UNIQUE (job_id, person_id, value)
);
CREATE INDEX IF NOT EXISTS job_contacts_job ON job_contacts(job_id);
CREATE TABLE IF NOT EXISTS data_sources (       -- public data files used on every prospect run
    id INTEGER PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,                  -- link or file path
    label TEXT,
    added_at TEXT NOT NULL,
    last_used TEXT
);
CREATE TABLE IF NOT EXISTS templates (          -- text message templates
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (           -- texts sent from your own phone
    id INTEGER PRIMARY KEY,
    person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    phone TEXT NOT NULL,
    channel TEXT NOT NULL,                     -- sms (opened texting app) | phone (sent via QR / copy)
    template_id INTEGER REFERENCES templates(id) ON DELETE SET NULL,
    body TEXT NOT NULL,
    sent_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_phone ON messages(phone);
-- Client pipeline: one row per company being worked.
CREATE TABLE IF NOT EXISTS pipeline (
    id INTEGER PRIMARY KEY,
    company_id INTEGER UNIQUE NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    stage TEXT NOT NULL DEFAULT 'prospect',
    contact_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    owner TEXT,
    next_step TEXT,
    due_date TEXT,                             -- YYYY-MM-DD
    lost_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activities (         -- timeline: notes, emails, texts, stage changes, replies
    id INTEGER PRIMARY KEY,
    company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
    person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,                        -- note | email | text | stage | reply | bounce | system
    body TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS activities_company ON activities(company_id);
-- Follow-up sequences: ordered steps sent N days after enrolment.
CREATE TABLE IF NOT EXISTS sequences (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sequence_steps (
    id INTEGER PRIMARY KEY,
    sequence_id INTEGER NOT NULL REFERENCES sequences(id) ON DELETE CASCADE,
    step_no INTEGER NOT NULL,
    day INTEGER NOT NULL,                      -- days after enrolment
    channel TEXT NOT NULL,                     -- email | text
    template_id INTEGER REFERENCES templates(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS enrollments (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
    sequence_id INTEGER NOT NULL REFERENCES sequences(id) ON DELETE CASCADE,
    step_no INTEGER NOT NULL DEFAULT 1,         -- next step to send
    status TEXT NOT NULL DEFAULT 'active',     -- active | replied | finished | stopped | unsubscribed
    started_at TEXT NOT NULL,
    next_due TEXT,                             -- YYYY-MM-DD
    UNIQUE (person_id, sequence_id)
);
CREATE TABLE IF NOT EXISTS emails (             -- emails sent or opened in your email app
    id INTEGER PRIMARY KEY,
    person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    enrollment_id INTEGER REFERENCES enrollments(id) ON DELETE SET NULL,
    step_no INTEGER,                           -- follow-up step this email was
    to_addr TEXT NOT NULL,
    subject TEXT,
    body TEXT NOT NULL,
    method TEXT NOT NULL,                      -- smtp | mailto
    message_id TEXT,
    sent_at TEXT NOT NULL
);
-- Loxo data used for off-limits checks (refreshed by "Sync with Loxo").
CREATE TABLE IF NOT EXISTS loxo_companies (
    loxo_id INTEGER PRIMARY KEY,
    name TEXT,
    name_norm TEXT,
    domain TEXT,
    open_jobs INTEGER NOT NULL DEFAULT 0,
    all_jobs INTEGER NOT NULL DEFAULT 0,
    last_placement TEXT,                       -- most recent placement start date
    placements INTEGER NOT NULL DEFAULT 0,
    status_name TEXT,                          -- Loxo company status, e.g. "Current Client"
    blocked INTEGER NOT NULL DEFAULT 0,        -- Loxo's own "blocked" flag
    blocked_until TEXT,
    owner TEXT,                                -- account owner in Loxo
    synced_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS loxo_companies_domain ON loxo_companies(domain);
CREATE INDEX IF NOT EXISTS loxo_companies_name ON loxo_companies(name_norm);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS blocked_companies (  -- staffing / recruiting firms: never stored again
    key TEXT PRIMARY KEY,                      -- company key or domain
    name TEXT,
    reason TEXT,
    added_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS contacts_company ON contacts(company_id);
CREATE INDEX IF NOT EXISTS jobs_company ON jobs(company_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_path() -> str:
    return os.environ.get("ZOOMOLLO_DB", "zoomollo.db")


# Columns added after the first release; created on open if missing.
MIGRATIONS = {
    "companies": {"city": "TEXT", "industry": "TEXT", "address": "TEXT", "phone": "TEXT",
                  "source": "TEXT",
                  "offlimits_reason": "TEXT", "offlimits_until": "TEXT"},  # set by you in Zoomollo
    "jobs": {"department": "TEXT", "description": "TEXT"},
    "templates": {"kind": "TEXT NOT NULL DEFAULT 'text'", "subject": "TEXT"},
    "loxo_companies": {"status_name": "TEXT", "blocked": "INTEGER NOT NULL DEFAULT 0", "blocked_until": "TEXT",
                       "owner": "TEXT"},
    "people": {"pushed_to_loxo": "INTEGER NOT NULL DEFAULT 0",
               "email_pattern": "TEXT",            # format behind the current guess
               "email_attempts": "INTEGER NOT NULL DEFAULT 0",
               "tried_emails": "TEXT"},             # JSON list of addresses that bounced
}


class DB:
    def __init__(self, path: str | None = None):
        self.path = path or default_path()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.conn.commit()
        if self.conn.execute("PRAGMA user_version").fetchone()[0] < 2:
            self.purge_excluded()
            self.conn.execute("PRAGMA user_version = 2")
            self.conn.commit()

    # --- exclusions (see exclusions.py) ----------------------------------------
    def purge_excluded(self) -> dict:
        """Remove generic inboxes, recruiting people and staffing companies already saved."""
        from .exclusions import is_excluded_email, is_recruiting_title, staffing_signal
        out = {"emails": 0, "people": 0, "companies": 0}
        for r in self.conn.execute("SELECT id, type, value, title FROM contacts").fetchall():
            if (r["type"] == "email" and is_excluded_email(r["value"])) or is_recruiting_title(r["title"]):
                self.conn.execute("DELETE FROM contacts WHERE id = ?", (r["id"],))
                out["emails"] += 1
        for r in self.conn.execute("SELECT id, title, email FROM people").fetchall():
            if is_recruiting_title(r["title"]):
                self.conn.execute("DELETE FROM people WHERE id = ?", (r["id"],))
                if r["email"]:
                    self.conn.execute("DELETE FROM contacts WHERE value = ?", (r["email"],))
                out["people"] += 1
            elif r["email"] and is_excluded_email(r["email"]):
                self.conn.execute("UPDATE people SET email = NULL, email_status = NULL WHERE id = ?", (r["id"],))
        self.conn.execute("DELETE FROM job_contacts WHERE reason IN ('careers inbox', 'recruiter')")
        for r in self.conn.execute("SELECT id, name, domain FROM companies").fetchall():
            why = staffing_signal(r["name"], domain=r["domain"])
            if why:
                self.block_company(r["id"], why)
                out["companies"] += 1
        self.conn.commit()
        return out

    # --- templates & settings ---------------------------------------------------
    def save_template(self, name: str, body: str, template_id: int | None = None, kind: str = "text",
                      subject: str | None = None) -> int:
        if template_id:
            self.conn.execute("UPDATE templates SET name = ?, body = ?, subject = ?, updated_at = ? WHERE id = ?",
                              (name, body, subject, now(), template_id))
        else:
            template_id = self.conn.execute(
                "INSERT INTO templates (name, body, kind, subject, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (name, body, kind, subject, now(), now())).lastrowid
        self.conn.commit()
        return template_id

    def templates(self, kind: str | None = "text") -> list[sqlite3.Row]:
        if kind is None:
            return self.conn.execute("SELECT * FROM templates ORDER BY id").fetchall()
        return self.conn.execute("SELECT * FROM templates WHERE COALESCE(kind, 'text') = ? ORDER BY id",
                                 (kind,)).fetchall()

    def template(self, template_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM templates WHERE id = ?", (template_id,)).fetchone()

    def delete_template(self, template_id: int) -> None:
        self.conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        self.conn.commit()

    def get_setting(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, value.strip()))
        self.conn.commit()

    def sources(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM data_sources ORDER BY id").fetchall()

    def add_source(self, url: str, label: str | None = None) -> int:
        from urllib.parse import urlparse
        label = label or urlparse(url).netloc or url.rsplit("/", 1)[-1]
        self.conn.execute("INSERT INTO data_sources (url, label, added_at) VALUES (?, ?, ?) "
                          "ON CONFLICT(url) DO UPDATE SET label = excluded.label", (url.strip(), label, now()))
        self.conn.commit()
        return self.conn.execute("SELECT id FROM data_sources WHERE url = ?", (url.strip(),)).fetchone()[0]

    def remove_source(self, source_id: int) -> None:
        self.conn.execute("DELETE FROM data_sources WHERE id = ?", (source_id,))
        self.conn.commit()

    def is_blocked(self, *keys: str | None) -> bool:
        keys = [k.lower() for k in keys if k]
        if not keys:
            return False
        q = f"SELECT 1 FROM blocked_companies WHERE key IN ({','.join('?' * len(keys))})"
        return self.conn.execute(q, keys).fetchone() is not None

    def block_company(self, company_id: int, reason: str) -> None:
        """Record a staffing/recruiting company and delete everything saved for it."""
        row = self.company_row(company_id)
        if not row:
            return
        for k in {row["key"], row["domain"]} - {None}:
            self.conn.execute("INSERT OR REPLACE INTO blocked_companies (key, name, reason, added_at) "
                              "VALUES (?, ?, ?, ?)", (k.lower(), row["name"], reason, now()))
        for table in ("contacts", "socials", "jobs", "people", "pattern_feedback"):
            self.conn.execute(f"DELETE FROM {table} WHERE company_id = ?", (company_id,))
        self.conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
        self.conn.commit()

    # --- companies ---------------------------------------------------------
    def company(self, key: str, domain: str | None = None, name: str | None = None) -> int:
        self.conn.execute(
            "INSERT INTO companies (key, domain, name, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET domain = COALESCE(companies.domain, excluded.domain), "
            "name = COALESCE(companies.name, excluded.name)",
            (key, domain, name, now()),
        )
        return self.conn.execute("SELECT id FROM companies WHERE key = ?", (key,)).fetchone()[0]

    def update_company(self, company_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE companies SET {cols} WHERE id = ?", (*fields.values(), company_id))

    # --- suppression -------------------------------------------------------
    def is_suppressed(self, value: str) -> bool:
        value = value.lower()
        checks = [value]
        if "@" in value:
            checks.append("@" + value.split("@", 1)[1])
        q = f"SELECT 1 FROM suppression WHERE value IN ({','.join('?' * len(checks))})"
        return self.conn.execute(q, checks).fetchone() is not None

    def suppress(self, value: str, reason: str = "opt-out") -> int:
        value = value.strip().lower()
        self.conn.execute(
            "INSERT OR REPLACE INTO suppression (value, reason, added_at) VALUES (?, ?, ?)",
            (value, reason, now()),
        )
        if value.startswith("@"):
            self.conn.execute("UPDATE people SET email = NULL, email_status = NULL WHERE email LIKE ?",
                              ("%" + value,))
            cur = self.conn.execute("DELETE FROM contacts WHERE type='email' AND value LIKE ?", ("%" + value,))
        else:
            self.conn.execute("UPDATE people SET email = NULL, email_status = NULL WHERE email = ?", (value,))
            self.conn.execute("UPDATE people SET phone = NULL WHERE phone = ?", (value,))
            cur = self.conn.execute("DELETE FROM contacts WHERE value = ?", (value,))
        self.conn.commit()
        return cur.rowcount

    # --- contacts ----------------------------------------------------------
    def add_contact(self, company_id: int | None, type_: str, value: str, kind: str,
                    name: str | None = None, title: str | None = None,
                    source_url: str | None = None) -> bool:
        """Insert or refresh a contact. Returns False if suppressed."""
        from .exclusions import is_excluded_email, is_recruiting_title
        value = value.lower() if type_ == "email" else value
        if self.is_suppressed(value) or is_recruiting_title(title):
            return False
        if type_ == "email" and (self.is_bad_email(value) or is_excluded_email(value)):
            return False
        t = now()
        self.conn.execute(
            "INSERT INTO contacts (company_id, type, value, name, title, kind, source_url, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(type, value) DO UPDATE SET last_seen = excluded.last_seen, "
            "name = COALESCE(contacts.name, excluded.name), "
            "title = COALESCE(contacts.title, excluded.title), "
            # A real sighting beats a guess.
            "kind = CASE WHEN contacts.kind = 'guessed' THEN excluded.kind ELSE contacts.kind END, "
            "source_url = COALESCE(contacts.source_url, excluded.source_url)",
            (company_id, type_, value, name, title, kind, source_url, t, t),
        )
        return True

    def add_person(self, company_id: int, name: str, title: str | None = None, **fields) -> int | None:
        """Insert or enrich a person. Existing values win except blanks get filled."""
        from .exclusions import is_excluded_email, is_recruiting_title
        from .orgmap import department, seniority
        name = " ".join(name.split())
        key = name.lower()
        email = fields.get("email")
        if email and (self.is_suppressed(email) or self.is_bad_email(email) or is_excluded_email(email)):
            email = None
            fields.pop("email", None)
        row = self.conn.execute("SELECT * FROM people WHERE company_id = ? AND name_key = ?",
                                (company_id, key)).fetchone()
        if is_recruiting_title(title):  # never keep recruiting / talent-acquisition people
            if row:
                self.conn.execute("DELETE FROM people WHERE id = ?", (row["id"],))
                if row["email"]:
                    self.conn.execute("DELETE FROM contacts WHERE value = ?", (row["email"],))
            return None
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO people (company_id, name, name_key, title, department, seniority, email, "
                "email_status, phone, linkedin_url, source, source_url, first_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (company_id, name, key, title, department(title), seniority(title), email,
                 fields.get("email_status") or ("found" if email else None), fields.get("phone"),
                 fields.get("linkedin_url"), fields.get("source"), fields.get("source_url"), now()))
            return cur.lastrowid
        upd = {}
        if title and not row["title"]:
            upd.update(title=title, department=department(title), seniority=seniority(title))
        for f in ("phone", "linkedin_url", "source_url"):
            if fields.get(f) and not row[f]:
                upd[f] = fields[f]
        # A found email replaces a guess.
        if email and (not row["email"] or (row["email_status"] == "guessed"
                                           and fields.get("email_status", "found") == "found")):
            upd.update(email=email, email_status=fields.get("email_status") or "found")
        if upd:
            cols = ", ".join(f"{k} = ?" for k in upd)
            self.conn.execute(f"UPDATE people SET {cols} WHERE id = ?", (*upd.values(), row["id"]))
        return row["id"]

    def people(self, company_id: int | None = None, q: str = "", department: str | None = None,
               hiring_only: bool = False, limit: int = 1000) -> list[sqlite3.Row]:
        sql = ("SELECT p.*, co.name AS company_name, co.key AS company_key, co.city AS company_city "
               "FROM people p JOIN companies co ON co.id = p.company_id WHERE 1=1")
        args: list = []
        if company_id is not None:
            sql += " AND p.company_id = ?"
            args.append(company_id)
        if q:
            like = f"%{q.lower()}%"
            sql += (" AND (p.name_key LIKE ? OR lower(COALESCE(p.title,'')) LIKE ? OR lower(co.key) LIKE ? "
                    "OR lower(COALESCE(co.name,'')) LIKE ? OR lower(COALESCE(co.industry,'')) LIKE ? "
                    "OR lower(COALESCE(co.city,'')) LIKE ?)")
            args += [like] * 6
        if department:
            sql += " AND p.department = ?"
            args.append(department)
        if hiring_only:
            sql += " AND p.hiring_for IS NOT NULL"
        sql += (" ORDER BY co.name, CASE p.seniority WHEN 'c_level' THEN 0 WHEN 'vp' THEN 1 "
                "WHEN 'director' THEN 2 WHEN 'manager' THEN 3 ELSE 4 END, p.name LIMIT ?")
        args.append(limit)
        return self.conn.execute(sql, args).fetchall()

    def company_row(self, company_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()

    def add_social(self, company_id: int, kind: str, url: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO socials (company_id, kind, url) VALUES (?, ?, ?)",
                          (company_id, kind, url))

    def add_job(self, company_id: int, board: str, external_id: str, title: str,
                location: str | None, url: str | None, posted_at: str | None,
                description: str | None = None) -> None:
        from .orgmap import department
        self.conn.execute(
            "INSERT INTO jobs (company_id, board, external_id, title, location, url, posted_at, "
            "first_seen, department, description) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(board, external_id) DO UPDATE SET "
            "location = COALESCE(jobs.location, excluded.location), "
            "posted_at = COALESCE(jobs.posted_at, excluded.posted_at), "
            "description = COALESCE(excluded.description, jobs.description)",
            (company_id, board, external_id, title, location, url, posted_at, now(), department(title),
             (description or None) and description[:20000]),
        )

    def is_bad_email(self, email: str) -> bool:
        return self.conn.execute("SELECT 1 FROM bad_emails WHERE value = ?", (email.lower(),)).fetchone() is not None

    def job_contacts(self, job_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT jc.*, p.name, p.title, p.email, p.email_status, p.phone, p.linkedin_url "
            "FROM job_contacts jc LEFT JOIN people p ON p.id = jc.person_id "
            "WHERE jc.job_id = ? ORDER BY jc.rank, jc.id", (job_id,)).fetchall()

    def named_emails(self, company_id: int) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT name, value FROM contacts WHERE company_id = ? AND type = 'email' "
            "AND name IS NOT NULL AND kind = 'person'", (company_id,))
        return [(r[0], r[1]) for r in rows]

    def search(self, q: str = "", type_: str | None = None, kind: str | None = None,
               limit: int = 500) -> list[sqlite3.Row]:
        sql = ("SELECT c.*, co.key AS company_key, co.domain AS company_domain, co.name AS company_name "
               "FROM contacts c LEFT JOIN companies co ON co.id = c.company_id WHERE 1=1")
        args: list = []
        if q:
            like = f"%{q.lower()}%"
            sql += (" AND (lower(c.value) LIKE ? OR lower(COALESCE(c.name,'')) LIKE ? "
                    "OR lower(COALESCE(c.title,'')) LIKE ? OR lower(COALESCE(co.key,'')) LIKE ? "
                    "OR lower(COALESCE(co.name,'')) LIKE ?)")
            args += [like] * 5
        if type_:
            sql += " AND c.type = ?"
            args.append(type_)
        if kind:
            sql += " AND c.kind = ?"
            args.append(kind)
        sql += " ORDER BY c.last_seen DESC LIMIT ?"
        args.append(limit)
        return self.conn.execute(sql, args).fetchall()

    def jobs(self, q: str = "", limit: int = 500, company_id: int | None = None) -> list[sqlite3.Row]:
        sql = ("SELECT j.*, co.key AS company_key, co.name AS company_name FROM jobs j "
               "LEFT JOIN companies co ON co.id = j.company_id")
        args: list = []
        sql += " WHERE 1=1"
        if company_id is not None:
            sql += " AND j.company_id = ?"
            args.append(company_id)
        if q:
            sql += (" AND (lower(j.title) LIKE ? OR lower(co.key) LIKE ? OR lower(COALESCE(co.name,'')) LIKE ? "
                    "OR lower(COALESCE(j.location,'')) LIKE ?)")
            args += [f"%{q.lower()}%"] * 4
        sql += " ORDER BY j.first_seen DESC LIMIT ?"
        args.append(limit)
        return self.conn.execute(sql, args).fetchall()

    def companies(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT co.*, "
            "(SELECT COUNT(*) FROM contacts c WHERE c.company_id = co.id AND c.type='email') AS emails, "
            "(SELECT COUNT(*) FROM contacts c WHERE c.company_id = co.id AND c.type='phone') AS phones, "
            "(SELECT COUNT(*) FROM jobs j WHERE j.company_id = co.id) AS open_jobs, "
            "(SELECT COUNT(*) FROM people p WHERE p.company_id = co.id) AS people "
            "FROM companies co ORDER BY co.created_at DESC").fetchall()

    def stats(self) -> dict:
        one = lambda q: self.conn.execute(q).fetchone()[0]  # noqa: E731
        return {
            "companies": one("SELECT COUNT(*) FROM companies"),
            "emails": one("SELECT COUNT(*) FROM contacts WHERE type='email' AND kind != 'guessed'"),
            "guessed": one("SELECT COUNT(*) FROM contacts WHERE kind = 'guessed'"),
            "phones": one("SELECT COUNT(*) FROM contacts WHERE type='phone'"),
            "people": one("SELECT COUNT(*) FROM people"),
            "jobs": one("SELECT COUNT(*) FROM jobs"),
        }

    def mark_pushed(self, contact_id: int) -> None:
        self.conn.execute("UPDATE contacts SET pushed_to_loxo = 1 WHERE id = ?", (contact_id,))
        self.conn.commit()

    def commit(self) -> None:
        self.conn.commit()
