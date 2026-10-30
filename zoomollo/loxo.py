"""Optional push of named contacts into Loxo as people records.

Needs LOXO_AGENCY_SLUG and LOXO_API_KEY (Loxo: Settings > API Keys).
LOXO_DOMAIN defaults to app.loxo.co.

Field names follow Loxo's public "create person" endpoint. Check them against
your account's API docs before a real run; use dry_run=True to see payloads.
"""

from __future__ import annotations

import os

import requests

from .db import DB


class LoxoClient:
    def __init__(self, agency_slug: str | None = None, api_key: str | None = None,
                 domain: str | None = None):
        self.agency_slug = agency_slug or os.environ.get("LOXO_AGENCY_SLUG")
        self.api_key = api_key or os.environ.get("LOXO_API_KEY")
        self.domain = domain or os.environ.get("LOXO_DOMAIN", "app.loxo.co")
        if not (self.agency_slug and self.api_key):
            raise RuntimeError("Set LOXO_AGENCY_SLUG and LOXO_API_KEY to use the Loxo integration.")
        self.base = f"https://{self.domain}/api/{self.agency_slug}"
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.api_key}",
                                     "Accept": "application/json"})

    def create_person(self, payload: dict) -> dict:
        r = self.session.post(f"{self.base}/people", data=payload, timeout=30)
        r.raise_for_status()
        return r.json()


def person_payload(name: str, email: str | None, phone: str | None, title: str | None,
                   company: str | None) -> dict:
    p = {"person[name]": name}
    if email:
        p["person[email]"] = email
    if phone:
        p["person[phone]"] = phone
    if title:
        p["person[title]"] = title
    if company:
        p["person[company]"] = company
    return p


def push_people(db: DB, dry_run: bool = True, include_guessed: bool = False,
                limit: int = 100) -> list[dict]:
    """Push named, not-yet-pushed contacts. Returns the payloads sent (or that would be)."""
    kinds = ("person", "guessed") if include_guessed else ("person",)
    rows = db.conn.execute(
        f"SELECT c.*, co.name AS company_name, co.key AS company_key FROM contacts c "
        f"LEFT JOIN companies co ON co.id = c.company_id "
        f"WHERE c.name IS NOT NULL AND c.pushed_to_loxo = 0 AND c.type = 'email' "
        f"AND c.kind IN ({','.join('?' * len(kinds))}) LIMIT ?", (*kinds, limit)).fetchall()
    client = None if dry_run else LoxoClient()
    sent = []
    for r in rows:
        phone = db.conn.execute(
            "SELECT value FROM contacts WHERE type='phone' AND name = ? AND company_id = ? LIMIT 1",
            (r["name"], r["company_id"])).fetchone()
        payload = person_payload(r["name"], r["value"], phone[0] if phone else None, r["title"],
                                 r["company_name"] or r["company_key"])
        if client:
            client.create_person(payload)
            db.mark_pushed(r["id"])
        sent.append(payload)
    return sent
