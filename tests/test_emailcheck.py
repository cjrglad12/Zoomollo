import json

from zoomollo import emailcheck
from zoomollo.db import DB


def setup(tmp_path):
    db = DB(str(tmp_path / "e.db"))
    cid = db.company("acme.com", domain="acme.com", name="Acme")
    db.update_company(cid, email_pattern="{first}.{last}")
    a = db.add_person(cid, "Robert Smith", "VP of Sales")
    b = db.add_person(cid, "Jane Doe", "Controller")
    db.commit()
    emailcheck.guess_all(db, cid)
    return db, cid, a, b


def person(db, pid):
    return db.conn.execute("SELECT * FROM people WHERE id = ?", (pid,)).fetchone()


def test_bounce_loop_until_failed(tmp_path):
    db, cid, a, _ = setup(tmp_path)
    assert person(db, a)["email"] == "robert.smith@acme.com"
    seen = ["robert.smith@acme.com"]
    for i in range(emailcheck.MAX_ATTEMPTS):
        [(name, nxt)] = emailcheck.mark_bad(db, person(db, a)["email"])
        assert name == "Robert Smith"
        if i < emailcheck.MAX_ATTEMPTS - 1:
            assert nxt and nxt not in seen
            seen.append(nxt)
        else:
            assert nxt is None
    p = person(db, a)
    assert p["email_status"] == "failed" and p["email"] is None and p["email_attempts"] == 10
    assert len(json.loads(p["tried_emails"])) == 10
    assert db.is_bad_email("robert.smith@acme.com")
    # Bounced guesses never come back, and aren't listed as contacts.
    assert not db.conn.execute("SELECT 1 FROM contacts WHERE value = 'robert.smith@acme.com'").fetchone()


def test_bounced_format_is_avoided_and_confirmed_format_spreads(tmp_path):
    db, cid, a, b = setup(tmp_path)
    assert person(db, b)["email"] == "jane.doe@acme.com"
    emailcheck.mark_bad(db, "robert.smith@acme.com")      # first.last bounced for Robert
    assert person(db, a)["email"] == "robert@acme.com"     # next most common format
    changed = emailcheck.mark_good(db, "robert@acme.com")  # that one worked
    assert person(db, a)["email_status"] == "confirmed"
    assert changed == 1 and person(db, b)["email"] == "jane@acme.com"  # colleague switched to {first}
    assert db.company_row(cid)["email_pattern"] == "{first}"


def test_import_bounces(tmp_path):
    db, cid, a, b = setup(tmp_path)
    text = ("From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\n"
            "Address not found: Your message wasn't delivered to jane.doe@acme.com because the address "
            "couldn't be found. random@other.com")
    res = emailcheck.import_bounces(db, text)
    assert res == [("jane.doe@acme.com", "Jane Doe", "jane@acme.com")]


def test_auto_verify(tmp_path):
    db, cid, a, _ = setup(tmp_path)

    class Fake(emailcheck.Verifier):
        name = "fake"
        calls = []

        def check(self, email):
            self.calls.append(email)
            return "valid" if email == "rsmith@acme.com" else "invalid"

    v = Fake()
    assert emailcheck.auto_verify(db, a, v) == ("rsmith@acme.com", "confirmed")
    assert v.calls == ["robert.smith@acme.com", "robert@acme.com", "rsmith@acme.com"]

    class CatchAll(emailcheck.Verifier):
        def check(self, email):
            return "catch_all"
    db2, _, a2, _ = setup(tmp_path / "x") if (tmp_path / "x").mkdir() is None else None
    assert emailcheck.auto_verify(db2, a2, CatchAll()) == ("robert.smith@acme.com", "catch_all")
