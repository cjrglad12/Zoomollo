from datetime import date

from zoomollo import crm, loxo, messaging
from zoomollo.db import DB
from zoomollo.offlimits import set_manual, status


class FakeLoxo:
    """Shapes as Loxo returns them: scroll-paged companies/placements, page-paged jobs."""

    def __init__(self):
        self.calls = []

    def get(self, endpoint, **params):
        self.calls.append((endpoint, params))
        if endpoint == "companies":
            if "scroll_id" not in params:
                return {"companies": [{"id": 1, "name": "Acme Mechanical", "url": "https://www.acme.com"},
                                      {"id": 2, "name": "Beta Air Inc"}], "scroll_id": "S1"}
            if params["scroll_id"] == "S1":
                return {"companies": [{"id": 3, "name": "Gamma HVAC", "url": "gamma.com"},
                                      {"id": 4, "name": "Delta Cooling"}], "scroll_id": "S2"}
            return {"companies": [], "scroll_id": None}
        if endpoint == "jobs":
            if params["page"] == 1:
                return {"results": [
                    {"id": 10, "title": "Service Tech", "status": {"id": 1, "name": "Active"}, "company": {"id": 1, "name": "Acme Mechanical"}},
                    {"id": 11, "title": "Estimator", "status": {"id": 2, "name": "Filled"}, "company": {"id": 3, "name": "Gamma HVAC"}},
                ], "current_page": 1, "total_pages": 1}
        if endpoint == "placements":
            if "scroll_id" not in params:
                return {"placements": [
                    {"id": 50, "start_date": date.today().replace(day=1).isoformat(), "job": {"id": 12, "company": {"id": 2, "name": "Beta Air Inc"}}},
                    {"id": 51, "start_date": "2019-03-01", "job": {"id": 11, "company": {"id": 3, "name": "Gamma HVAC"}}},
                ], "scroll_id": "P1"}
            return {"placements": []}
        return {}

    scroll = loxo.LoxoClient.scroll
    pages = loxo.LoxoClient.pages


def setup(tmp_path):
    db = DB(str(tmp_path / "o.db"))
    ids = {}
    for key, name in [("acme.com", "Acme Mechanical"), ("betaair.com", "Beta Air"), ("gamma.com", "Gamma HVAC"),
                      ("delta.com", "Delta Cooling LLC"), ("new.com", "Brand New Co")]:
        ids[key] = db.company(key, domain=key, name=name)
    db.commit()
    r = loxo.sync(db, client=FakeLoxo())
    return db, ids, r


def test_sync_and_rules(tmp_path):
    db, ids, r = setup(tmp_path)
    assert r == {"companies": 4, "jobs": 2, "placements": 2, "full": True}
    acme, beta, gamma, delta, new = (status(db, ids[k]) for k in ("acme.com", "betaair.com", "gamma.com", "delta.com", "new.com"))
    assert acme.blocked and "1 open job" in acme.reason             # matched by domain
    assert beta.blocked and "Placed into" in beta.reason             # matched by name ("Beta Air Inc" ~ "Beta Air")
    assert gamma.level == "warn"                                       # only filled jobs / old placement
    assert delta.level == "info"                                       # in Loxo, no jobs ("LLC" ignored)
    assert new.level is None


def test_manual_offlimits_with_end_date(tmp_path):
    db, ids, _ = setup(tmp_path)
    set_manual(db, ids["new.com"], "partner agency's client", "2099-01-01")
    assert status(db, ids["new.com"]).blocked
    set_manual(db, ids["new.com"], "old deal", "2000-01-01")
    assert not status(db, ids["new.com"]).blocked                      # expired
    set_manual(db, ids["new.com"], None)
    assert status(db, ids["new.com"]).level is None


def test_blocked_companies_are_kept_out_of_outreach(tmp_path):
    db, ids, _ = setup(tmp_path)
    a = db.add_person(ids["acme.com"], "Ann Acme", "Service Manager", phone="+15125550101", email="ann@acme.com",
                      email_status="found")
    n = db.add_person(ids["new.com"], "Ned New", "Owner", phone="+15125550102", email="ned@new.com", email_status="found")
    db.commit()
    assert [p["name"] for p in messaging.outreach_list(db, hiring_only=False)] == ["Ned New"]
    crm.ensure_starters(db)
    seq = crm.sequences(db)[0]["id"]
    try:
        crm.enroll(db, a, seq)
        raise AssertionError("should be blocked")
    except crm.OffLimits as e:
        assert "Active client" in str(e)
    crm.enroll(db, n, seq)
    try:
        crm.add_to_pipeline(db, ids["acme.com"])
        raise AssertionError("should be blocked")
    except crm.OffLimits:
        pass
    crm.add_to_pipeline(db, ids["acme.com"], force=True)               # explicit override
    assert [p["person"]["name"] for p in loxo.push_people(db, dry_run=True)] == ["Ned New"]


def test_slug_can_be_pasted_as_an_address():
    from zoomollo.loxo import parse_slug
    assert parse_slug("acme") == ("acme", None)
    assert parse_slug("app.loxo.co/acme") == ("acme", "app.loxo.co")
    assert parse_slug("https://app.loxo.co/api/acme/") == ("acme", "app.loxo.co")


class FakeLoxoStatuses(FakeLoxo):
    def get(self, endpoint, **params):
        if endpoint == "companies" and "scroll_id" not in params:
            return {"companies": [
                {"id": 7, "name": "Client Co", "company_global_status": {"id": 1, "name": "Current Client"},
                 "owned_by_name": "Casey"},
                {"id": 8, "name": "Opp Co", "company_global_status": {"id": 2, "name": "Active Opportunity"},
                 "owned_by_name": "Sam"},
                {"id": 9, "name": "Blocked Co", "blocked": True, "blocked_until": "2099-01-01"},
                {"id": 10, "name": "Mail Co", "emails": [{"value": "ops@gmail.com"}, {"value": "jo@mailco.io"}]},
            ], "scroll_id": "Z"}
        if endpoint == "companies":
            return {"companies": []}
        return {"results": []} if endpoint == "jobs" else {"placements": []}


def test_loxo_statuses_block_flag_and_email_domains(tmp_path):
    db = DB(str(tmp_path / "s.db"))
    ids = {n: db.company(k, domain=k, name=n) for k, n in
           [("clientco.com", "Client Co"), ("oppco.com", "Opp Co"), ("blockedco.com", "Blocked Co"),
            ("mailco.io", "Totally Different Name")]}
    db.commit()
    loxo.sync(db, client=FakeLoxoStatuses())
    c, o, b, m = (status(db, ids[n]) for n in ("Client Co", "Opp Co", "Blocked Co", "Totally Different Name"))
    assert c.blocked and c.reason == "Current Client in Loxo (owner: Casey)"
    assert o.level == "warn" and "owner: Sam" in o.reason
    assert b.blocked and "until 2099-01-01" in b.reason
    assert m.level == "info"                                           # matched via jo@mailco.io, not gmail


def test_loxo_get_retries_dropped_connections(monkeypatch):
    import requests
    c = loxo.LoxoClient(agency_slug="acme", api_key="k")
    calls = []

    class R:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True}

    def flaky(url, **kw):
        calls.append(url)
        if len(calls) < 3:
            raise requests.ConnectionError("dropped")
        return R()

    monkeypatch.setattr(c.session, "get", flaky)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert c.get("companies") == {"ok": True} and len(calls) == 3


def test_quick_sync_keeps_company_details(tmp_path):
    db, ids, _ = setup(tmp_path)
    db.conn.execute("UPDATE loxo_companies SET status_name = 'Current Client', owner = 'Casey' WHERE loxo_id = 4")
    db.commit()
    fake = FakeLoxo()
    r = loxo.sync(db, client=fake, full=False)
    assert r["full"] is False and not any(ep == "companies" for ep, _ in fake.calls)   # no company scan
    row = db.conn.execute("SELECT * FROM loxo_companies WHERE loxo_id = 4").fetchone()
    assert row["status_name"] == "Current Client" and row["owner"] == "Casey"          # kept from full sync
    assert status(db, ids["acme.com"]).blocked                                         # jobs refreshed


def test_auto_sync_schedule(tmp_path, monkeypatch):
    db, ids, _ = setup(tmp_path)
    monkeypatch.setenv("LOXO_AGENCY_SLUG", "acme")
    monkeypatch.setenv("LOXO_API_KEY", "k")
    calls = []
    monkeypatch.setattr(loxo, "sync", lambda db, progress=None, full=True: calls.append(full) or {"full": full})
    assert loxo.auto_sync(db) is None                       # full + quick both fresh
    db.set_setting("loxo_synced_at", "2000-01-01T00:00:00")
    loxo.auto_sync(db)
    db.set_setting("loxo_full_synced_at", "2000-01-01T00:00:00")
    loxo.auto_sync(db)
    assert calls == [False, True]
