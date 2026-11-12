"""Crawl and import tasks stored in the database, so a restart does not drop them.

The web UI keeps a small in-memory wake-up queue. The rows here are the source
of truth: the same action and item are not queued twice while one is waiting
or running, and anything left running when the process died is picked up again
on the next start.
"""

from __future__ import annotations

import json
import sqlite3

from .db import DB, now

# User-started work. Hourly jobs (inbox, auto-send) stay in memory: they are
# scheduled again on their own and should not be replayed after a restart.
DURABLE = {"crawl", "addco", "directory", "jobs", "prospect"}


def enqueue(db: DB, action: str, item: str, opts: dict | None = None) -> int | None:
    """Insert a pending task. Returns its id, or None if that task is already waiting or running."""
    clean = {k: v for k, v in (opts or {}).items() if not str(k).startswith("_")}
    try:
        cur = db.conn.execute(
            "INSERT INTO task_queue (action, item, opts, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
            (action, item, json.dumps(clean), now()))
        db.conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        db.conn.rollback()
        return None


def claim(db: DB, task_id: int) -> bool:
    """Mark a pending task running. False if another worker already took it or it finished."""
    cur = db.conn.execute(
        "UPDATE task_queue SET status = 'running' WHERE id = ? AND status = 'pending'", (task_id,))
    db.conn.commit()
    return cur.rowcount == 1


def keep(db: DB, task_id: int, opts: dict) -> None:
    """A retry stays running (this process still owns it) and remembers how many tries it has had."""
    clean = {k: v for k, v in opts.items() if k in ("_tries",) or not str(k).startswith("_")}
    db.conn.execute("UPDATE task_queue SET opts = ? WHERE id = ?", (json.dumps(clean), task_id))
    db.conn.commit()


def finish(db: DB, task_id: int, status: str) -> None:
    db.conn.execute("UPDATE task_queue SET status = ? WHERE id = ?", (status, task_id))
    db.conn.commit()


def recover(db: DB) -> list[tuple[str, str, dict]]:
    """After a restart: tasks left running were interrupted, so they wait again.
    Returns every pending task as (action, item, opts) with opts['_task_id'] set."""
    db.conn.execute("UPDATE task_queue SET status = 'pending' WHERE status = 'running'")
    db.conn.commit()
    out = []
    for row in db.conn.execute("SELECT * FROM task_queue WHERE status = 'pending' ORDER BY id"):
        opts = json.loads(row["opts"] or "{}")
        opts["_task_id"] = row["id"]
        out.append((row["action"], row["item"], opts))
    return out
