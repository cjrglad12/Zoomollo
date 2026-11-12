from datetime import date, timedelta

from zoomollo import leads
from zoomollo.db import DB
from zoomollo.hiringscore import save_desk


class R:
    def __init__(self, status, data=None, text=""):
        self.status_code, self._data, self.text = status, data, text

    def json(self):
        return self._data


def setup(tmp_path, monkeypatch, credits="5"):
    monkeypatch.setenv("APOLLO_API_KEY", "test-key")
    monkeypatch.setenv("APOLLO_FREE_CREDITS", credits)
    db = DB(str(tmp_path / "a.db"))
    save_desk(db, "controller")
    ids = []
    for i in range(3):
        cid = db.company(f"co{i}.test", domain=f"co{i}.test", name=f"Co {i}")
        db.update_company(cid, city="Dallas, TX", last_crawled=date.today().isoformat())
        db.add_job(cid, "website", f"j{i}", "Controller", "Dallas, TX", None, (date.today() - timedelta(days=40)).isoformat())
        leads.save(db, cid, [{"id": f"ap{i}", "first_name": "Dana", "last_name_obfuscated": "Ex***e",
                              "title": "VP Finance"}])
        ids.append(cid)
    db.commit()
    return db, ids


def lead_id(db, cid):
    return db.conn.execute("SELECT id FROM leads WHERE company_id = ?", (cid,)).fetchone()[0]


def test_enrich_adds_contact_without_paid_reveals(tmp_path, monkeypatch):
    db, ids = setup(tmp_path, monkeypatch)
    sent = []

    def fake_post(url, **kw):
        sent.append((url, kw["json"]))
        return R(200, {"person": {"last_name": "Example", "email": "dana.example@co0.test", "email_status": "verified",
                                  "title": "VP Finance", "linkedin_url": "https://www.linkedin.com/in/dana-example"}})
    monkeypatch.setattr(leads.requests, "post", fake_post)
    r = leads.enrich(db, lead_id(db, ids[0]))
    assert r["ok"] and "Dana Example" in r["msg"]
    url, body = sent[0]
    assert url == leads.MATCH_URL and body == {"id": "ap0", "reveal_personal_emails": False, "reveal_phone_number": False}
    p = db.conn.execute("SELECT * FROM people WHERE company_id = ?", (ids[0],)).fetchone()
    assert (p["name"], p["email"], p["email_status"], p["source"]) == ("Dana Example", "dana.example@co0.test", "found", "apollo")
    assert leads.credits_used(db) == 1 and leads.credits_left(db) == 4
    assert leads.enrich(db, lead_id(db, ids[0]))["msg"] == "Already looked up."          # never charged twice
    assert len(sent) == 1


def test_budget_daily_spread_and_out_of_credits(tmp_path, monkeypatch):
    db, ids = setup(tmp_path, monkeypatch, credits="40")       # 40/month -> 2 a day
    calls = []

    def fake_post(url, **kw):
        calls.append(kw["json"]["id"])
        return R(200, {"person": {"last_name": "Example", "email": None, "email_status": "unavailable"}})
    monkeypatch.setattr(leads.requests, "post", fake_post)
    assert leads.daily_allowance() == 2
    assert leads.auto_enrich(db) == 2 and len(calls) == 2                               # stops at today's share
    assert leads.auto_enrich(db) == 0
    # Apollo says the free credits are gone: pause for the month, no more calls.
    db.set_setting("apollo_credits_" + __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%d"), "0")
    monkeypatch.setattr(leads.requests, "post", lambda url, **kw: R(422, text="Insufficient credits. Please upgrade your plan."))
    r = leads.enrich(db, lead_id(db, ids[2]))
    assert not r["ok"] and "paused until next month" in r["msg"] and leads.credits_left(db) == 0
    monkeypatch.setattr(leads.requests, "post", lambda url, **kw: (_ for _ in ()).throw(AssertionError("called")))
    assert "No free Apollo credits left" in leads.enrich(db, lead_id(db, ids[2]))["msg"]


def test_zero_budget_turns_lookups_off_and_button_shows(tmp_path, monkeypatch):
    from zoomollo.web import create_app
    db, ids = setup(tmp_path, monkeypatch, credits="0")
    monkeypatch.setattr(leads.requests, "post", lambda url, **kw: (_ for _ in ()).throw(AssertionError("called")))
    assert leads.auto_enrich(db) == 0 and leads.credits_left(db) == 0
    monkeypatch.setenv("APOLLO_FREE_CREDITS", "5")
    page = create_app(db.path).test_client().get(f"/company/{ids[1]}").data.decode()
    assert "Get full name &amp; email" in page and "5 free Apollo credits left this month" in page
