"""SQLite storage. One file, no server."""

from __future__ import annotations

import os
import sqlite3
import threading
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
CREATE TABLE IF NOT EXISTS person_channels (   -- every email / phone known for a person (Loxo returns several)
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK (type IN ('email', 'phone')),
    value TEXT NOT NULL,
    label TEXT,                         -- Mobile, Work, Direct, Personal...
    source TEXT,
    added_at TEXT NOT NULL,
    UNIQUE (person_id, type, value)
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
CREATE TABLE IF NOT EXISTS loxo_outbox (        -- activities waiting to be (or already) logged in Loxo
    id INTEGER PRIMARY KEY,
    activity_id INTEGER,                         -- the Zoomollo activity it came from
    company_id INTEGER,
    person_id INTEGER,                           -- Zoomollo contact (people.id)
    candidate_id INTEGER,                        -- for "Pitched" on the candidate's Loxo record
    activity_type TEXT NOT NULL,                 -- Loxo activity type name, e.g. "Phone Call"
    notes TEXT,
    happened_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',      -- pending / sent / skipped / failed
    detail TEXT,                                 -- why skipped or failed, or the Loxo event id
    tries INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
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
CREATE TABLE IF NOT EXISTS email_opens (        -- "X opened your email" / "Read:" receipts from your inbox
    id INTEGER PRIMARY KEY,
    email TEXT,                                -- who opened it
    name TEXT,
    person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    candidate_id INTEGER REFERENCES candidates(id) ON DELETE SET NULL,
    company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    subject TEXT,                              -- the email of yours they opened
    message_id TEXT UNIQUE,                    -- the notification, so it's counted once
    opened_at TEXT NOT NULL
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
-- What changed at a company between checks (feeds the "This week" view and the hiring score).
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,                        -- job_new | job_reposted | job_gone | person_new
    detail TEXT,
    job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_created ON events(created_at);
-- Candidates (from Loxo or uploaded résumés). Résumés stay on this computer.
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,                      -- loxo | upload
    loxo_id INTEGER UNIQUE,
    name TEXT,
    title TEXT,
    company TEXT,
    city TEXT,
    state TEXT,
    email TEXT,
    phone TEXT,
    linkedin_url TEXT,
    skills TEXT,                               -- skills / tags / summary from Loxo
    profile TEXT,                              -- work history / education from Loxo
    resume_text TEXT,
    resume_name TEXT,
    resume_status TEXT,                        -- fetched | none | failed: <why> | queued
    resume_fetched_at TEXT,
    updated_at TEXT,                           -- last change in Loxo
    created_at TEXT NOT NULL
);
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
CREATE TABLE IF NOT EXISTS task_queue (       -- crawls and imports waiting to run (survives a restart)
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,                      -- crawl | addco | directory | jobs | prospect
    item TEXT NOT NULL,                        -- domain, board, or "what in where"
    opts TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',    -- pending | running | done | failed
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS task_queue_active ON task_queue(action, item)
    WHERE status IN ('pending', 'running');
"""


def now_precise() -> str:
    """Microsecond timestamp: tells "seen this check" from "seen last check" even seconds apart."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_path() -> str:
    return os.environ.get("ZOOMOLLO_DB", "zoomollo.db")


# Columns added after the first release; created on open if missing.
MIGRATIONS = {
    "companies": {"abroad_note": "TEXT",            # site says much of the team is abroad (geo.py)
                  "city": "TEXT", "industry": "TEXT", "address": "TEXT", "phone": "TEXT",
                  "source": "TEXT",
                  "offlimits_reason": "TEXT", "offlimits_until": "TEXT",  # set by you in Zoomollo
                  "recruiters_seen": "INTEGER NOT NULL DEFAULT 0",  # in-house recruiters spotted (not saved)
                  "last_checked": "TEXT",           # last full look at its site / job boards
                  "summary": "TEXT",                   # one tight line: what the company does
                  "summary_source": "TEXT",            # website | you
                  "headcount": "INTEGER",           # total employees, when known
                  "headcount_source": "TEXT",       # website | apollo | you
                  "crawl_failures": "INTEGER NOT NULL DEFAULT 0",  # site didn't load, in a row
                  "crawl_backoff_until": "TEXT"},   # don't retry a failed site before this
    "jobs": {"department": "TEXT", "description": "TEXT",
             "last_seen": "TEXT",                  # last time the role was still listed
             "salary_max": "INTEGER",              # yearly, parsed from the post when stated
             "closed_at": "TEXT",                  # stopped being listed (2 checks in a row)
             "missed_checks": "INTEGER NOT NULL DEFAULT 0"},
    "templates": {"kind": "TEXT NOT NULL DEFAULT 'text'", "subject": "TEXT"},
    "enrollments": {"auto": "INTEGER NOT NULL DEFAULT 0"},  # 1 = started by hourly auto-prospecting
    "loxo_companies": {"status_name": "TEXT", "blocked": "INTEGER NOT NULL DEFAULT 0", "blocked_until": "TEXT",
                       "owner": "TEXT"},
    "people": {"pushed_to_loxo": "INTEGER NOT NULL DEFAULT 0",
               "email_pattern": "TEXT",            # format behind the current guess
               "email_attempts": "INTEGER NOT NULL DEFAULT 0",
               "tried_emails": "TEXT",              # JSON list of addresses that bounced
               "loxo_person_id": "INTEGER",         # this contact's record in Loxo, once matched or created
               "loxo_added_at": "TEXT",             # added / linked to Loxo for contact info (loxoenrich)
               "loxo_pulled_at": "TEXT",            # last read back from Loxo
               "review_note": "TEXT"},              # shared email/phone or a Loxo link we refused
}


_SETUP_LOCK = threading.Lock()
_READY: set[str] = set()  # database files this program has already set up


class DB:
    def __init__(self, path: str | None = None):
        self.path = path or default_path()
        # Wait up to a minute for another task's write (e.g. a big Loxo sync) instead of
        # failing with "database is locked" after the default 5 seconds.
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=60)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")  # safe with WAL, and much less waiting on the disk
        if self.path in _READY and self.conn.execute("PRAGMA user_version").fetchone()[0] >= 8:  # tables and repairs were already checked by this program: opening is instant
            return
        self._setup()
        if self.path != ":memory:":
            _READY.add(self.path)

    def stamp(self) -> tuple:
        """Changes whenever anything is written to the database file. Costs nothing to read."""
        out = []
        for p in (self.path, self.path + "-wal"):
            try:
                st = os.stat(p)
                out.append((st.st_mtime_ns, st.st_size))
            except OSError:
                out.append(None)
        return tuple(out)

    def _setup(self) -> None:
        with _SETUP_LOCK:  # the web page and background jobs may open a new database at the same moment
            self.conn.executescript(SCHEMA)
            for table, cols in MIGRATIONS.items():
                have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                for col, typ in cols.items():
                    if col not in have:
                        try:
                            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                        except sqlite3.OperationalError as e:
                            if "duplicate column" not in str(e):  # another process added it first
                                raise
            # Lets "has the candidate list changed?" be answered from a small index, not 20,000 résumés.
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_candidates_version "
                              "ON candidates(updated_at, resume_fetched_at)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_candidates_source ON candidates(source)")
            self.conn.commit()
        if self.conn.execute("PRAGMA user_version").fetchone()[0] < 2:
            self.purge_excluded()
            self.conn.execute("PRAGMA user_version = 2")
            self.conn.commit()
        if self.conn.execute("PRAGMA user_version").fetchone()[0] < 3:
            with _SETUP_LOCK:
                if self.conn.execute("PRAGMA user_version").fetchone()[0] < 3:
                    self.fix_saved_data()
                    self.conn.execute("PRAGMA user_version = 3")
                    self.conn.commit()
        if self.conn.execute("PRAGMA user_version").fetchone()[0] < 4:
            with _SETUP_LOCK:
                if self.conn.execute("PRAGMA user_version").fetchone()[0] < 4:
                    self.fix_guess_labels()
                    self.conn.execute("PRAGMA user_version = 4")
                    self.conn.commit()
        if self.conn.execute("PRAGMA user_version").fetchone()[0] < 5:
            with _SETUP_LOCK:
                if self.conn.execute("PRAGMA user_version").fetchone()[0] < 5:
                    self.merge_duplicate_domains()
                    self.conn.execute("PRAGMA user_version = 5")
                    self.conn.commit()
        if self.conn.execute("PRAGMA user_version").fetchone()[0] < 6:
            with _SETUP_LOCK:
                if self.conn.execute("PRAGMA user_version").fetchone()[0] < 6:
                    self.fix_chief_levels()
                    self.conn.execute("PRAGMA user_version = 6")
                    self.conn.commit()
        if self.conn.execute("PRAGMA user_version").fetchone()[0] < 7:
            with _SETUP_LOCK:
                if self.conn.execute("PRAGMA user_version").fetchone()[0] < 7:
                    self.purge_support()
                    self.conn.execute("PRAGMA user_version = 7")
                    self.conn.commit()
        if self.conn.execute("PRAGMA user_version").fetchone()[0] < 8:
            with _SETUP_LOCK:
                if self.conn.execute("PRAGMA user_version").fetchone()[0] < 8:
                    self.fix_shared_personal_emails()
                    self.conn.execute("PRAGMA user_version = 8")
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

    def purge_support(self) -> dict:
        """Remove customer service / tech support numbers and reps already saved: numbers
        read from help, support or billing pages, and people with CSR or support-rep titles."""
        from .exclusions import is_support_page, is_support_title
        out = {"numbers": 0, "people": 0}
        for r in self.conn.execute("SELECT DISTINCT value, source_url FROM contacts WHERE type = 'phone' "
                                   "AND kind != 'person'").fetchall():
            if is_support_page(r["source_url"]) and not self.is_suppressed(r["value"]):
                self.suppress(r["value"], "customer service line")
                out["numbers"] += 1
        for r in self.conn.execute("SELECT id, title, email, phone FROM people").fetchall():
            if is_support_title(r["title"]):
                self.conn.execute("DELETE FROM people WHERE id = ?", (r["id"],))
                for v in (r["email"], r["phone"]):
                    if v:
                        self.conn.execute("DELETE FROM contacts WHERE value = ? AND kind = 'person'", (v,))
                out["people"] += 1
        for r in self.conn.execute("SELECT id, title FROM contacts WHERE title IS NOT NULL").fetchall():
            if is_support_title(r["title"]):
                self.conn.execute("DELETE FROM contacts WHERE id = ?", (r["id"],))
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
        from .exclusions import is_excluded_email, is_unwanted_title
        value = value.lower() if type_ == "email" else value
        if self.is_suppressed(value) or is_unwanted_title(title):
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
        from .exclusions import is_excluded_email, is_recruiting_title, is_support_title, works_elsewhere
        from .orgmap import department, seniority
        name = " ".join(name.split())
        if is_support_title(title):  # a CSR or tech support rep: can't help with a hire
            self.conn.execute("DELETE FROM people WHERE company_id = ? AND name_key = ?", (company_id, name.lower()))
            return None
        if (title and fields.get("source") == "website"
                and works_elsewhere(title, *self._co_name_domain(company_id))):
            return None  # a customer / partner quoted on their site, not an employee
        key = name.lower()
        email = fields.get("email")
        if email and (self.is_suppressed(email) or self.is_bad_email(email) or is_excluded_email(email)):
            email = None
            fields.pop("email", None)
        row = self.conn.execute("SELECT * FROM people WHERE company_id = ? AND name_key = ?",
                                (company_id, key)).fetchone()
        if is_recruiting_title(title):  # never keep recruiting / talent-acquisition people
            # ...but remember the company has one: it lowers the odds they need an agency.
            self.conn.execute("UPDATE companies SET recruiters_seen = recruiters_seen + 1 WHERE id = ?",
                              (company_id,))
            if row:
                self.conn.execute("DELETE FROM people WHERE id = ?", (row["id"],))
                if row["email"]:
                    self.conn.execute("DELETE FROM contacts WHERE value = ?", (row["email"],))
            return None
        review = None
        status = fields.get("email_status") or ("found" if email else None)
        if email and status in ("found", "confirmed"):
            email, review = self._claim_found_email(company_id, name, key, email)
            if email is None:
                fields.pop("email", None)
                fields.pop("email_status", None)
        if row is None:
            level = seniority(title)
            co = self.company_row(company_id)
            cur = self.conn.execute(
                "INSERT INTO people (company_id, name, name_key, title, department, seniority, email, "
                "email_status, phone, linkedin_url, source, source_url, first_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (company_id, name, key, title, department(title), seniority(title), email,
                 fields.get("email_status") or ("found" if email else None), fields.get("phone"),
                 fields.get("linkedin_url"), fields.get("source"), fields.get("source_url"), now()))
            if co and co["last_checked"] and level in ("c_level", "vp", "director", "manager") and title:
                self.add_event(company_id, "person_new", f"New on their team: {name}, {title}",
                               person_id=cur.lastrowid)
            self._set_review(cur.lastrowid, review)
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
        self._set_review(row["id"], review)
        return row["id"]

    def _set_review(self, person_id: int | None, note: str | None) -> None:
        if person_id and note:
            self.conn.execute("UPDATE people SET review_note = ? WHERE id = ?", (note[:300], person_id))

    def _claim_found_email(self, company_id: int, name: str, name_key: str, email: str) -> tuple[str | None, str | None]:
        """A personal email found on a site belongs to one person. Returns (email or None, review note).

        bsmiley@ stays with Bill Smiley. It is not also stored on Zach Miller, and if Zach
        already has it the address moves to Bill. Two people who both match (Ann Smith and
        Adam Smith, asmith@) are flagged and the address is not copied onto the second.
        """
        from .patterns import email_matches_name
        email = email.lower().strip()
        me_matches = email_matches_name(email, name)
        holders = self.conn.execute(
            "SELECT id, name FROM people WHERE company_id = ? AND name_key != ? AND lower(email) = ?",
            (company_id, name_key, email)).fetchall()
        if holders:
            holder = holders[0]
            holder_matches = email_matches_name(email, holder["name"])
            if me_matches and not holder_matches:
                note = f"Shared email {email} kept for {name}; removed from {holder['name']}"
                self.conn.execute(
                    "UPDATE people SET email = NULL, email_status = NULL, review_note = ? WHERE id = ?",
                    (note[:300], holder["id"]))
                return email, note
            note = f"Shared email {email} already on {holder['name']}; not also stored on {name}"
            self._set_review(holder["id"], note)
            return None, note
        if not me_matches:
            better = self.conn.execute(
                "SELECT id, name FROM people WHERE company_id = ? AND name_key != ?",
                (company_id, name_key)).fetchall()
            better = [p for p in better if email_matches_name(email, p["name"])]
            if better:
                note = f"Shared email {email} looks like {better[0]['name']}, not {name}"
                self._set_review(better[0]["id"],
                                 f"Shared email {email} also appeared with {name}; not stored on both")
                return None, note
        return email, None

    def fix_shared_personal_emails(self) -> int:
        """One-time repair: a found/confirmed personal email stored on two people at one company."""
        from .patterns import email_matches_name
        changed = 0
        groups = self.conn.execute(
            "SELECT company_id, lower(email) AS email FROM people "
            "WHERE email IS NOT NULL AND COALESCE(email_status, '') IN ('found', 'confirmed') "
            "GROUP BY company_id, lower(email) HAVING COUNT(*) > 1").fetchall()
        for company_id, email in groups:
            people = list(self.conn.execute(
                "SELECT id, name FROM people WHERE company_id = ? AND lower(email) = ? ORDER BY id",
                (company_id, email)))
            matches = [p for p in people if email_matches_name(email, p["name"])]
            keep = matches[0]["id"] if len(matches) == 1 else (None if len(matches) > 1 else people[0]["id"])
            for person in people:
                if keep is not None and person["id"] == keep:
                    self._set_review(person["id"], f"Shared email {email} kept here; it was also on someone else")
                    continue
                self.conn.execute(
                    "UPDATE people SET email = NULL, email_status = NULL, review_note = ? WHERE id = ?",
                    (f"Shared email {email} removed; it was on more than one person"[:300], person["id"]))
                changed += 1
        self.conn.commit()
        return changed

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
        sql += " ORDER BY "
        if q:  # the person or title you typed first; people who only match on company or city after
            sql += ("CASE WHEN p.name_key LIKE ? THEN 0 WHEN lower(COALESCE(p.title,'')) LIKE ? THEN 1 ELSE 2 END, ")
            args += [like, like]
        sql += ("co.name, CASE p.seniority WHEN 'c_level' THEN 0 WHEN 'vp' THEN 1 "
                "WHEN 'director' THEN 2 WHEN 'manager' THEN 3 ELSE 4 END, p.name LIMIT ?")
        args.append(limit)
        return self.conn.execute(sql, args).fetchall()

    def company_row(self, company_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()

    def _co_name_domain(self, company_id: int) -> tuple[str | None, str | None]:
        r = self.conn.execute("SELECT name, domain FROM companies WHERE id = ?", (company_id,)).fetchone()
        return (r["name"], r["domain"]) if r else (None, None)

    def set_website(self, company_id: int, domain: str) -> int:
        """Give a company found without a website (e.g. from permits) its site.
        Returns the company id to crawl: this one, or the existing company that
        already has that website (this one's pipeline card and notes move to it)."""
        from .crawler import normalize_domain
        domain = normalize_domain(domain)
        other = self.conn.execute("SELECT id FROM companies WHERE key = ? OR domain = ?", (domain, domain)).fetchone()
        if other and other["id"] != company_id:
            keep = other["id"]
            if not self.conn.execute("SELECT 1 FROM pipeline WHERE company_id = ?", (keep,)).fetchone():
                self.conn.execute("UPDATE pipeline SET company_id = ? WHERE company_id = ?", (keep, company_id))
            for t in ("activities", "events"):
                self.conn.execute(f"UPDATE {t} SET company_id = ? WHERE company_id = ?", (keep, company_id))
            self.conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
            self.conn.commit()
            return keep
        self.conn.execute("UPDATE companies SET key = ?, domain = ? WHERE id = ?", (domain, domain, company_id))
        self.conn.commit()
        return company_id

    def fix_saved_data(self) -> dict:
        """One-time repairs: websites saved with a port ("site.com:443"), and
        customers / partners quoted on a site that were saved as its staff."""
        from .crawler import normalize_domain
        from .exclusions import works_elsewhere
        out = {"domains": 0, "people": 0}
        for r in self.conn.execute("SELECT id, key, domain FROM companies WHERE domain LIKE '%:%'").fetchall():
            d = normalize_domain(r["domain"])
            taken = self.conn.execute("SELECT 1 FROM companies WHERE key = ? AND id != ?", (d, r["id"])).fetchone()
            key = d if (r["key"] == r["domain"] and not taken) else r["key"]
            self.conn.execute("UPDATE companies SET domain = ?, key = ? WHERE id = ?", (d, key, r["id"]))
            out["domains"] += 1
        for r in self.conn.execute("SELECT p.id, p.title, p.email, c.name, c.domain FROM people p "
                                   "JOIN companies c ON c.id = p.company_id WHERE p.source = 'website' AND "
                                   "(p.title LIKE '%,%' OR p.title LIKE '% at %' OR p.title LIKE '% - %' "
                                   "OR p.title LIKE '%|%')").fetchall():
            if works_elsewhere(r["title"], r["name"], r["domain"]):
                self.conn.execute("DELETE FROM people WHERE id = ?", (r["id"],))
                if r["email"]:
                    self.conn.execute("DELETE FROM contacts WHERE value = ? AND kind = 'guessed'", (r["email"],))
                out["people"] += 1
        self.conn.commit()
        return out

    def merge_companies(self, keep: int, drop: int) -> None:
        """Fold company `drop` into `keep`: people, contacts, jobs, history. Same-named people
        are combined (the kept record gains any title, email or phone it was missing)."""
        if keep == drop:
            return
        tables = [r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        cols = {t: {r[1] for r in self.conn.execute(f"PRAGMA table_info({t})")} for t in tables}
        for p in self.conn.execute("SELECT * FROM people WHERE company_id = ?", (drop,)).fetchall():
            twin = self.conn.execute("SELECT * FROM people WHERE company_id = ? AND name_key = ?",
                                     (keep, p["name_key"])).fetchone()
            if not twin:
                continue
            fill = {k: p[k] for k in ("title", "email", "email_status", "email_pattern", "phone", "linkedin",
                                      "department", "seniority", "hiring_for")
                    if k in p.keys() and p[k] and not twin[k] and not (k.startswith("email_") and twin["email"])}
            if fill:
                self.conn.execute(f"UPDATE people SET {', '.join(f'{k} = ?' for k in fill)} WHERE id = ?",
                                  (*fill.values(), twin["id"]))
            for t in tables:
                if "person_id" in cols[t]:
                    self.conn.execute(f"UPDATE OR IGNORE {t} SET person_id = ? WHERE person_id = ?", (twin["id"], p["id"]))
                    self.conn.execute(f"DELETE FROM {t} WHERE person_id = ?", (p["id"],))
            self.conn.execute("DELETE FROM people WHERE id = ?", (p["id"],))
        for t in tables:
            if "company_id" in cols[t]:
                self.conn.execute(f"UPDATE OR IGNORE {t} SET company_id = ? WHERE company_id = ?", (keep, drop))
                self.conn.execute(f"DELETE FROM {t} WHERE company_id = ?", (drop,))
        k, d = (self.conn.execute("SELECT * FROM companies WHERE id = ?", (i,)).fetchone() for i in (keep, drop))
        fill = {c: d[c] for c in ("city", "industry", "address", "phone", "email_pattern")
                if c in d.keys() and d[c] and not k[c]}
        if fill:
            self.update_company(keep, **fill)
        self.conn.execute("DELETE FROM companies WHERE id = ?", (drop,))
        self.conn.commit()

    def fix_chief_levels(self) -> int:
        """One-time repair: "Survey Party Chief" and "Crew Chief" were saved as C-level executives.
        Re-read the level of every title with "chief" in it; anyone demoted stops being a hiring manager."""
        from .orgmap import seniority
        n = 0
        for r in self.conn.execute("SELECT id, title, seniority FROM people WHERE title LIKE '%chief%'").fetchall():
            level = seniority(r["title"])
            if level != r["seniority"]:
                self.conn.execute("UPDATE people SET seniority = ?, hiring_for = NULL WHERE id = ?", (level, r["id"]))
                n += 1
        return n

    def merge_duplicate_domains(self) -> int:
        """Companies saved twice under one website become one (the copy with open jobs wins,
        then the one with the shorter name)."""
        n = 0
        for (domain,) in self.conn.execute("SELECT domain FROM companies WHERE domain IS NOT NULL AND domain != '' "
                                           "GROUP BY domain HAVING COUNT(*) > 1").fetchall():
            rows = self.conn.execute(
                "SELECT c.id, c.key, c.name, (SELECT COUNT(*) FROM jobs j WHERE j.company_id = c.id) AS jobs "
                "FROM companies c WHERE c.domain = ?", (domain,)).fetchall()
            rows.sort(key=lambda r: (r["key"] != domain, -r["jobs"], len(r["name"] or "")))
            for r in rows[1:]:
                self.merge_companies(rows[0]["id"], r["id"])
                n += 1
        return n

    def fix_guess_labels(self) -> int:
        """One-time repair: guesses that were wrongly labelled "found" (matched against
        our own guesses) go back to "guessed". Addresses seen on a real page stay "found"."""
        n = self.conn.execute(
            "UPDATE people SET email_status = 'guessed' WHERE email_status = 'found' "
            "AND EXISTS (SELECT 1 FROM contacts c WHERE c.value = people.email AND c.kind = 'guessed') "
            "AND NOT EXISTS (SELECT 1 FROM contacts c WHERE c.value = people.email "
            "AND COALESCE(c.kind, '') != 'guessed')").rowcount
        self.conn.commit()
        return n

    def add_social(self, company_id: int, kind: str, url: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO socials (company_id, kind, url) VALUES (?, ?, ?)",
                          (company_id, kind, url))

    def add_job(self, company_id: int, board: str, external_id: str, title: str,
                location: str | None, url: str | None, posted_at: str | None,
                description: str | None = None) -> None:
        from .hiringscore import parse_salary
        from .orgmap import department
        t = now_precise()
        before = self.conn.execute("SELECT id, closed_at FROM jobs WHERE board = ? AND external_id = ?",
                                   (board, external_id)).fetchone()
        self.conn.execute(
            "INSERT INTO jobs (company_id, board, external_id, title, location, url, posted_at, "
            "first_seen, last_seen, department, description, salary_max) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(board, external_id) DO UPDATE SET "
            "location = COALESCE(jobs.location, excluded.location), "
            "posted_at = COALESCE(jobs.posted_at, excluded.posted_at), "
            "description = COALESCE(excluded.description, jobs.description), "
            "salary_max = COALESCE(excluded.salary_max, jobs.salary_max), "
            "last_seen = excluded.last_seen, closed_at = NULL, missed_checks = 0",
            (company_id, board, external_id, title, location, url, posted_at, t, t, department(title),
             (description or None) and description[:20000], parse_salary(description or "")),
        )
        self._job_events(company_id, board, external_id, title, before)

    def _job_events(self, company_id, board, external_id, title, before) -> None:
        """New / reposted roles. Nothing is recorded on the first look at a company."""
        co = self.company_row(company_id)
        if not co or not co["last_checked"]:
            return
        job_id = self.conn.execute("SELECT id FROM jobs WHERE board = ? AND external_id = ?",
                                   (board, external_id)).fetchone()[0]
        if before and before["closed_at"]:
            self.add_event(company_id, "job_reposted", f"{title} is listed again (was closed {before['closed_at'][:10]})",
                           job_id=job_id)
        elif not before:
            norm = " ".join(title.lower().split())
            closed = self.conn.execute(
                "SELECT closed_at FROM jobs WHERE company_id = ? AND id != ? AND closed_at IS NOT NULL "
                "AND closed_at >= date('now', '-120 days') AND lower(trim(title)) = ? ORDER BY closed_at DESC LIMIT 1",
                (company_id, job_id, norm)).fetchone()
            if closed:
                self.add_event(company_id, "job_reposted", f"{title} reposted (closed {closed[0][:10]})", job_id=job_id)
            else:
                self.add_event(company_id, "job_new", f"New role: {title}", job_id=job_id)

    def add_event(self, company_id: int, kind: str, detail: str, job_id: int | None = None,
                  person_id: int | None = None) -> None:
        self.conn.execute("INSERT INTO events (company_id, kind, detail, job_id, person_id, created_at) "
                          "VALUES (?, ?, ?, ?, ?, ?)", (company_id, kind, detail, job_id, person_id, now()))
        if self.conn.execute("SELECT 1 FROM pipeline WHERE company_id = ?", (company_id,)).fetchone():
            self.conn.execute("INSERT INTO activities (company_id, person_id, kind, body, created_at) "
                              "VALUES (?, ?, 'change', ?, ?)", (company_id, person_id, detail, now()))

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

    def jobs(self, q: str = "", limit: int = 500, company_id: int | None = None,
             include_closed: bool = False) -> list[sqlite3.Row]:
        sql = ("SELECT j.*, co.key AS company_key, co.name AS company_name FROM jobs j "
               "LEFT JOIN companies co ON co.id = j.company_id")
        args: list = []
        sql += " WHERE 1=1" if include_closed else " WHERE j.closed_at IS NULL"
        if company_id is not None:
            sql += " AND j.company_id = ?"
            args.append(company_id)
        if q:
            sql += (" AND (lower(j.title) LIKE ? OR lower(co.key) LIKE ? OR lower(COALESCE(co.name,'')) LIKE ? "
                    "OR lower(COALESCE(j.location,'')) LIKE ?)")
            args += [f"%{q.lower()}%"] * 4
            sql += " ORDER BY CASE WHEN lower(j.title) LIKE ? THEN 0 ELSE 1 END, j.first_seen DESC LIMIT ?"
            args.append(f"%{q.lower()}%")
        else:
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
