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
CREATE INDEX IF NOT EXISTS contacts_company ON contacts(company_id);
CREATE INDEX IF NOT EXISTS jobs_company ON jobs(company_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_path() -> str:
    return os.environ.get("ZOOMOLLO_DB", "zoomollo.db")


class DB:
    def __init__(self, path: str | None = None):
        self.path = path or default_path()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

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
            cur = self.conn.execute("DELETE FROM contacts WHERE type='email' AND value LIKE ?", ("%" + value,))
        else:
            cur = self.conn.execute("DELETE FROM contacts WHERE value = ?", (value,))
        self.conn.commit()
        return cur.rowcount

    # --- contacts ----------------------------------------------------------
    def add_contact(self, company_id: int | None, type_: str, value: str, kind: str,
                    name: str | None = None, title: str | None = None,
                    source_url: str | None = None) -> bool:
        """Insert or refresh a contact. Returns False if suppressed."""
        value = value.lower() if type_ == "email" else value
        if self.is_suppressed(value):
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

    def add_social(self, company_id: int, kind: str, url: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO socials (company_id, kind, url) VALUES (?, ?, ?)",
                          (company_id, kind, url))

    def add_job(self, company_id: int, board: str, external_id: str, title: str,
                location: str | None, url: str | None, posted_at: str | None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO jobs (company_id, board, external_id, title, location, url, posted_at, first_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (company_id, board, external_id, title, location, url, posted_at, now()),
        )

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

    def jobs(self, q: str = "", limit: int = 500) -> list[sqlite3.Row]:
        sql = ("SELECT j.*, co.key AS company_key, co.name AS company_name FROM jobs j "
               "LEFT JOIN companies co ON co.id = j.company_id")
        args: list = []
        if q:
            sql += " WHERE lower(j.title) LIKE ? OR lower(co.key) LIKE ? OR lower(COALESCE(j.location,'')) LIKE ?"
            args += [f"%{q.lower()}%"] * 3
        sql += " ORDER BY j.first_seen DESC LIMIT ?"
        args.append(limit)
        return self.conn.execute(sql, args).fetchall()

    def companies(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT co.*, "
            "(SELECT COUNT(*) FROM contacts c WHERE c.company_id = co.id AND c.type='email') AS emails, "
            "(SELECT COUNT(*) FROM contacts c WHERE c.company_id = co.id AND c.type='phone') AS phones, "
            "(SELECT COUNT(*) FROM jobs j WHERE j.company_id = co.id) AS open_jobs "
            "FROM companies co ORDER BY co.created_at DESC").fetchall()

    def stats(self) -> dict:
        one = lambda q: self.conn.execute(q).fetchone()[0]  # noqa: E731
        return {
            "companies": one("SELECT COUNT(*) FROM companies"),
            "emails": one("SELECT COUNT(*) FROM contacts WHERE type='email' AND kind != 'guessed'"),
            "guessed": one("SELECT COUNT(*) FROM contacts WHERE kind = 'guessed'"),
            "phones": one("SELECT COUNT(*) FROM contacts WHERE type='phone'"),
            "jobs": one("SELECT COUNT(*) FROM jobs"),
        }

    def mark_pushed(self, contact_id: int) -> None:
        self.conn.execute("UPDATE contacts SET pushed_to_loxo = 1 WHERE id = ?", (contact_id,))
        self.conn.commit()

    def commit(self) -> None:
        self.conn.commit()
