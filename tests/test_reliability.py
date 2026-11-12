"""Timeouts, a durable crawl queue, failure backoff, Loxo channel push, and match flags."""

import threading
import time

import requests

from zoomollo.db import DB


def test_slow_request_is_abandoned(monkeypatch):
    """A page that trickles past the per-request limit is dropped, not waited out."""
    from zoomollo.crawler import Crawler

    def sleepy(self, url, **kw):
        time.sleep(2)
        raise requests.ConnectionError(url)

    monkeypatch.setattr("requests.Session.get", sleepy)
    crawler = Crawler(timeout=0.3, budget=5)
    started = time.monotonic()
    assert crawler.fetch("https://aa.com/") is None
    assert time.monotonic() - started < 1.5


def test_company_budget_fails_and_backs_off(tmp_path, monkeypatch):
    """One hung site is abandoned inside its time budget and will not be retried immediately."""
    from zoomollo import pipeline

    def sleepy(self, url, **kw):
        time.sleep(5)
        raise requests.ConnectionError(url)

    monkeypatch.setattr("requests.Session.get", sleepy)
    monkeypatch.setattr(pipeline, "has_mx", lambda domain: False)
    db = DB(str(tmp_path / "b.db"))
    started = time.monotonic()
    result = pipeline.crawl_company(db, "aa.com", max_pages=10, delay=0, budget=0.4, timeout=0.25)
    assert time.monotonic() - started < 2
    assert result["pages"] == 0 and result.get("gave_up")
    row = db.conn.execute(
        "SELECT last_checked, crawl_failures, crawl_backoff_until FROM companies WHERE key = 'aa.com'").fetchone()
    assert row["last_checked"] is None
    assert row["crawl_failures"] == 1 and row["crawl_backoff_until"]


def test_failed_sites_stop_eating_the_hourly_budget(site, tmp_path):
    from zoomollo import pipeline, recheck

    site["ok.test/"] = "<title>Ok Co</title><p>Hello</p>"
    db = DB(str(tmp_path / "r.db"))
    dead = db.company("down.test", domain="down.test", name="Down")
    live = db.company("ok.test", domain="ok.test", name="Ok")
    db.update_company(live, last_checked="2000-01-01T00:00:00+00:00")
    db.commit()
    # The dead site is tried, records a failure, and is not due again while backed off.
    recheck.run(db, limit=1, delay=0)
    assert db.company_row(dead)["last_checked"] is None
    assert db.company_row(dead)["crawl_failures"] == 1
    assert [c["domain"] for c in recheck.due(db, days=0)] == ["ok.test"]
    again = recheck.run(db, limit=1, delay=0)
    assert again["checked"] == 1
    assert db.company_row(live)["last_checked"]
    assert db.company_row(dead)["crawl_failures"] == 1  # not retried, so the budget reached Ok


def test_crawl_queue_survives_restart_without_duplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOOMOLLO_NO_BACKGROUND", "1")
    from zoomollo import workqueue
    from zoomollo.web import create_app

    db = DB(str(tmp_path / "q.db"))
    assert workqueue.enqueue(db, "crawl", "aa.com", {})
    assert workqueue.enqueue(db, "crawl", "aa.com", {}) is None
    assert workqueue.enqueue(db, "crawl", "bb.com", {})
    aa = db.conn.execute("SELECT id FROM task_queue WHERE item = 'aa.com'").fetchone()[0]
    assert workqueue.claim(db, aa)
    assert [item for _, item, _ in workqueue.recover(db)] == ["aa.com", "bb.com"]

    seen = []
    started = threading.Event()
    release = threading.Event()

    def fake_crawl(db, domain, **kwargs):
        seen.append(domain)
        if not started.is_set():
            started.set()
            release.wait(3)
        return {"pages": 1, "emails": 0, "people": 0, "jobs": 0, "phones": 0, "blocked": None}

    monkeypatch.setattr("zoomollo.pipeline.crawl_company", fake_crawl)
    client = create_app(db.path).test_client()
    assert started.wait(2)
    client.post("/", data={"action": "crawl", "items": "aa.com\naa.com\ncc.com"})
    release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and seen.count("cc.com") < 1:
        time.sleep(0.02)
    assert seen.count("aa.com") == 1
    assert seen.count("bb.com") == 1 and seen.count("cc.com") == 1


def test_slow_crawl_does_not_block_the_web_ui(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOOMOLLO_NO_BACKGROUND", "1")
    from zoomollo.web import create_app

    started = threading.Event()
    release = threading.Event()

    def fake_crawl(db, domain, **kwargs):
        started.set()
        release.wait(2)
        return {"pages": 1, "emails": 0, "people": 0, "jobs": 0, "phones": 0, "blocked": None}

    monkeypatch.setattr("zoomollo.pipeline.crawl_company", fake_crawl)
    client = create_app(str(tmp_path / "u.db")).test_client()
    t0 = time.monotonic()
    response = client.post("/", data={"action": "crawl", "items": "slow.test"})
    assert response.status_code in (302, 200)
    assert time.monotonic() - t0 < 0.5
    assert started.wait(2)
    t1 = time.monotonic()
    assert client.get("/prospect").status_code == 200
    assert time.monotonic() - t1 < 1
    release.set()


def test_same_personal_email_is_not_stored_on_two_people(tmp_path):
    db = DB(str(tmp_path / "m.db"))
    cid = db.company("acme.test", domain="acme.test", name="Acme")
    db.add_person(cid, "Zach Miller", "Controller", email="bsmiley@acme.test", email_status="found", source="website")
    db.add_person(cid, "Bill Smiley", "CFO", email="bsmiley@acme.test", email_status="found", source="website")
    db.commit()
    rows = {p["name"]: p for p in db.people(cid)}
    assert rows["Bill Smiley"]["email"] == "bsmiley@acme.test"
    assert rows["Zach Miller"]["email"] is None
    assert rows["Zach Miller"]["review_note"] and "bsmiley@acme.test" in rows["Zach Miller"]["review_note"]
    assert rows["Bill Smiley"]["review_note"]


def test_scrape_does_not_give_one_email_to_two_people(site, tmp_path):
    from zoomollo import pipeline

    site["northwind.test/"] = (
        "<title>Northwind</title>"
        '<a href="mailto:bsmiley@northwind.test">Zach Miller</a>'
        "<h3>Bill Smiley</h3><p>Chief Financial Officer</p>"
        "<h3>Zach Miller</h3><p>Controller</p>"
    )
    db = DB(str(tmp_path / "s.db"))
    pipeline.crawl_company(db, "northwind.test", max_pages=5, delay=0)
    rows = {p["name"]: p for p in db.people()}
    owners = [name for name, p in rows.items() if (p["email"] or "") == "bsmiley@northwind.test"]
    assert owners == ["Bill Smiley"]
    assert rows["Zach Miller"]["review_note"]


def test_different_names_are_not_merged_in_loxo_on_a_shared_email_or_phone(tmp_path):
    from zoomollo.loxosync import Sync

    db = DB(str(tmp_path / "l.db"))
    cid = db.company("acme.test", domain="acme.test", name="Acme")
    zach = db.add_person(cid, "Zach Miller", "Controller", email="bsmiley@acme.test",
                         email_status="found", phone="+12145550199", source="website")
    db.commit()
    created = []

    class Client:
        base = "https://loxo.test/api/acme"

        def get(self, endpoint, **params):
            if endpoint == "person_types":
                return {"person_types": [{"id": 7, "name": "Contact"}]}
            if endpoint == "people":
                q = params.get("query") or ""
                if "bsmiley" in q or "2145550199" in q:
                    return {"people": [{"id": 55, "name": "Bill Smiley",
                                        "emails": [{"value": "bsmiley@acme.test"}],
                                        "phones": [{"value": "+1 214-555-0199"}]}]}
                return {"people": []}
            return {}

        def __init__(self):
            outer = self

            class S:
                def post(self, url, json=None, timeout=None):
                    created.append(json)

                    class R:
                        status_code = 200
                        content = b"{}"
                        text = ""

                        def raise_for_status(self):
                            pass

                        def json(self):
                            return {"person": {"id": 900}}
                    return R()
            outer.session = S()

    lid, how = Sync(db, Client()).find_or_create(zach)
    assert lid != 55
    note = db.conn.execute("SELECT review_note, loxo_person_id FROM people WHERE id = ?", (zach,)).fetchone()
    assert note["loxo_person_id"] != 55
    assert note["review_note"] and "Bill Smiley" in note["review_note"]
    if created:
        person = created[0]["person"]
        assert person.get("email") in (None, "")
        assert person.get("phone") in (None, "")
        assert person["name"] == "Zach Miller"


def test_push_channels_only_adds_missing_and_is_idempotent(tmp_path, monkeypatch):
    from zoomollo import loxoenrich

    db = DB(str(tmp_path / "p.db"))
    cid = db.company("acme.test", domain="acme.test", name="Acme")
    pid = db.add_person(cid, "Dana Example", "CFO", email="dana@acme.test", email_status="found",
                        phone="+12145550177", source="website")
    db.conn.execute("UPDATE people SET loxo_person_id = 55 WHERE id = ?", (pid,))
    db.conn.execute(
        "INSERT INTO person_channels (person_id, type, value, label, source, added_at) "
        "VALUES (?, 'email', 'old@acme.test', 'Work', 'loxo', '2020-01-01')", (pid,))
    db.commit()
    record = {"id": 55, "name": "Dana Example",
              "emails": [{"value": "old@acme.test"}], "phones": [{"value": "(214) 555-0100"}]}
    posts = []

    class Client:
        base = "https://loxo.test/api/acme"

        def get(self, endpoint, **params):
            assert "enrich" not in endpoint
            return {"person": record}

        def __init__(self):
            outer = self

            class S:
                def post(self, url, json=None, timeout=None):
                    assert "enrich" not in url
                    posts.append((url, json))
                    if url.endswith("/emails"):
                        record["emails"].append({"value": json["person_email"]["value"]})
                    elif url.endswith("/phones"):
                        record["phones"].append({"value": json["person_phone"]["value"]})
                    else:
                        raise AssertionError(url)

                    class R:
                        status_code = 200
                        content = b"{}"
                        text = ""
                        headers = {}

                        def raise_for_status(self):
                            pass

                        def json(self):
                            return {}
                    return R()
            outer.session = S()

    client = Client()
    added = loxoenrich.push_channels(db, pid, client)
    assert set(added["added"]) == {"dana@acme.test", "+12145550177"}
    assert "old@acme.test" not in added["added"]
    assert all(body.get("person_email", body.get("person_phone", {})).get("value") != "old@acme.test"
               for _, body in posts)
    assert record["emails"][0]["value"] == "old@acme.test"  # nothing already there was replaced
    again = loxoenrich.push_channels(db, pid, client)
    assert again["added"] == [] and len(posts) == 2

    monkeypatch.setenv("LOXO_AUTO_ADD", "0")
    assert loxoenrich.auto(db, client=client)["added"] == 0
    assert loxoenrich.push_channels(db, pid, client)["added"] == []


def test_push_channels_waits_for_retry_after(tmp_path, monkeypatch):
    from zoomollo import loxoenrich

    waits = []
    monkeypatch.setattr("time.sleep", lambda s: waits.append(s))
    db = DB(str(tmp_path / "w.db"))
    cid = db.company("acme.test", domain="acme.test", name="Acme")
    pid = db.add_person(cid, "Dana Example", "CFO", email="dana@acme.test", email_status="found")
    other = db.add_person(cid, "Lee Example", "VP", email="lee@acme.test", email_status="found")
    db.conn.execute("UPDATE people SET loxo_person_id = 55 WHERE id = ?", (pid,))
    db.conn.execute("UPDATE people SET loxo_person_id = 56 WHERE id = ?", (other,))
    db.commit()
    calls = []

    class Client:
        base = "https://loxo.test/api/acme"

        def get(self, endpoint, **params):
            return {"person": {"id": 55, "emails": [], "phones": []}}

        def __init__(self):
            class S:
                def post(self, url, json=None, timeout=None):
                    calls.append(url)

                    class Slow:
                        status_code = 429
                        headers = {"Retry-After": "3"}
                        content = b""
                        text = "slow down"

                        def raise_for_status(self):
                            import requests
                            err = requests.HTTPError(response=self)
                            raise err
                    return Slow()
            self.session = S()

    result = loxoenrich.push_linked(db, client=Client(), dry_run=False, limit=10)
    assert waits == [3.0] * 4  # four waits, then the fifth 429 stops the batch
    assert len(calls) == 5
    assert result["rate_limited"] is True
    assert result["people"] == 1  # the second person waits for the next run


def test_push_channels_endpoint_and_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ZOOMOLLO_NO_BACKGROUND", "1")
    monkeypatch.setenv("LOXO_AGENCY_SLUG", "acme")
    monkeypatch.setenv("LOXO_API_KEY", "k")
    from zoomollo import loxo as loxomod
    from zoomollo.cli import main
    from zoomollo.web import create_app

    db = DB(str(tmp_path / "e.db"))
    cid = db.company("acme.test", domain="acme.test", name="Acme")
    pid = db.add_person(cid, "Dana Example", "CFO", email="dana@acme.test", email_status="found")
    db.conn.execute("UPDATE people SET loxo_person_id = 55 WHERE id = ?", (pid,))
    db.commit()
    posts = []

    class Client:
        base = "https://loxo.test/api/acme"

        def get(self, endpoint, **params):
            return {"person": {"id": 55, "emails": [], "phones": []}}

        def __init__(self):
            class S:
                def post(self, url, json=None, timeout=None):
                    posts.append((url, json))

                    class R:
                        status_code = 200
                        content = b"{}"
                        text = ""
                        headers = {}

                        def raise_for_status(self):
                            pass

                        def json(self):
                            return {}
                    return R()
            self.session = S()

    monkeypatch.setattr(loxomod, "LoxoClient", lambda *a, **k: Client())
    response = create_app(db.path).test_client().post(f"/person/{pid}/loxo-channels")
    assert response.status_code == 302
    assert posts == [("https://loxo.test/api/acme/people/55/emails", {"person_email": {"value": "dana@acme.test"}})]

    monkeypatch.delenv("LOXO_AGENCY_SLUG", raising=False)
    monkeypatch.delenv("LOXO_API_KEY", raising=False)
    assert main(["--db", str(tmp_path / "cli.db"), "loxo-channels"]) == 0
    assert "isn't connected" in capsys.readouterr().out


def test_shared_crawler_has_no_total_time_limit(monkeypatch):
    """A directory read or prospect search shares one crawler across many sites; the
    per-company limit must not cut it off after the first few minutes."""
    from zoomollo import crawler as cr

    shared, one_company = cr.Crawler(), cr.Crawler(budget=180)
    shared._ensure_deadline()
    one_company._ensure_deadline()
    later = time.monotonic() + 3600
    monkeypatch.setattr(cr.time, "monotonic", lambda: later)
    assert not shared._over()
    assert one_company._over()


def test_credentials_after_a_name_are_the_same_person():
    from zoomollo.patterns import email_matches_name, same_person, split_name

    assert split_name("John Smith, CPA") == ("john", "smith")
    assert split_name("Jane Doe PE") == ("jane", "doe")
    assert split_name("Jane Doe, P.E., LEED AP") == ("jane", "doe")
    assert split_name("Robert Lee Jr") == ("robert", "lee")
    assert split_name("Mai Pe") == ("mai", "pe")  # a surname, not a credential
    assert same_person("John Smith, CPA", "John Smith")
    assert same_person("Jane Doe PE", "Jane Doe")
    assert same_person("Bill Smiley", "William Smiley")
    assert not same_person("Zach Miller", "Bill Smiley")
    assert email_matches_name("jsmith@acme.test", "John Smith, CPA")


def test_loxo_record_with_credentials_links_instead_of_duplicating(tmp_path):
    from zoomollo.loxosync import Sync

    db = DB(str(tmp_path / "c.db"))
    cid = db.company("acme.test", domain="acme.test", name="Acme")
    pid = db.add_person(cid, "John Smith", "Tax Partner", email="jsmith@acme.test",
                        email_status="found", source="website")
    db.commit()
    created = []

    class Client:
        base = "https://loxo.test/api/acme"

        def get(self, endpoint, **params):
            if endpoint == "person_types":
                return {"person_types": [{"id": 7, "name": "Contact"}]}
            if endpoint == "people":
                return {"people": [{"id": 77, "name": "John Smith, CPA",
                                    "emails": [{"value": "jsmith@acme.test"}]}]}
            return {}

        def __init__(self):
            class S:
                def post(self, url, json=None, timeout=None):
                    created.append(json)
                    raise AssertionError("should link, not create")
            self.session = S()

    lid, how = Sync(db, Client()).find_or_create(pid)
    assert lid == 77 and not created


def test_loxo_channels_works_through_everyone_across_runs(tmp_path):
    from zoomollo import loxoenrich

    db = DB(str(tmp_path / "n.db"))
    cid = db.company("acme.test", domain="acme.test", name="Acme")
    ids = []
    for i, name in enumerate(["Ann One", "Ben Two", "Cal Three"]):
        pid = db.add_person(cid, name, "VP", source="website")
        db.conn.execute("UPDATE people SET loxo_person_id = ? WHERE id = ?", (100 + i, pid))
        ids.append(pid)
    db.commit()
    seen = []

    class Client:
        base = "https://loxo.test/api/acme"

        def get(self, endpoint, **params):
            seen.append(endpoint)
            return {"person": {"emails": [], "phones": []}}

        session = None

    first = loxoenrich.push_linked(db, client=Client(), dry_run=False, limit=2)
    second = loxoenrich.push_linked(db, client=Client(), dry_run=False, limit=2)
    assert first["people"] == 2 and second["people"] == 2
    assert "people/102" in seen  # the third person was reached on the second run
