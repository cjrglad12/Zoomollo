from datetime import date, timedelta

from zoomollo import loxoenrich, today
from zoomollo.db import DB
from zoomollo.geo import abroad_reason, offshore_phrase, where
from zoomollo.hiringscore import load_desk, save_desk, score_all


def test_where_a_role_is():
    for loc in ("Dallas, TX", "Plano, Texas, United States", "Remote - US", "Paris, TX", "Dublin, OH",
                "Indianapolis, IN", "New York, NY 10001"):
        assert where(loc) == "us", loc
    for loc in ("Bengaluru, Karnataka, India", "Pune, IN", "Manila", "Toronto, ON, CA", "London, United Kingdom",
                "Mexico City, Mexico", "Remote, India", "Krakow, Poland"):
        assert where(loc) == "abroad", loc
    assert where("Remote") is None and where("") is None


def test_twenty_percent_abroad_is_a_bad_prospect():
    assert abroad_reason(["Dallas, TX", "Plano, TX", "Pune, India", "Dallas, TX", "Remote"])     # 1 of 4 = 25%
    assert not abroad_reason(["Dallas, TX"] * 5 + ["Pune, India"])                              # 1 of 6 = 17%
    assert not abroad_reason(["Pune, India", "Dallas, TX"])                                     # too few to judge
    assert abroad_reason([], "offshore development center") and not abroad_reason([], None)
    assert offshore_phrase("We run an offshore development center in Pune.")
    assert offshore_phrase("with our team in the Philippines") and offshore_phrase("Global Delivery Centers")
    assert not offshore_phrase("Our team in Dallas serves clients nationwide.")


def test_abroad_company_drops_off_today(tmp_path):
    db = DB(str(tmp_path / "g.db"))
    save_desk(db, "controller")
    recent = (date.today() - timedelta(days=5)).isoformat()
    ids = {}
    for name, extra in (("Local", ["Dallas, TX", "Plano, TX"]), ("Offshore", ["Hyderabad, India", "Pune, India"]),
                        ("Site", ["Dallas, TX"])):
        cid = db.company(f"{name.lower()}.test", domain=f"{name.lower()}.test", name=name)
        db.update_company(cid, city="Dallas, TX", last_crawled=date.today().isoformat())
        db.add_job(cid, "website", f"{name}c", "Controller", "Dallas, TX", None, recent)
        for i, loc in enumerate(extra):
            db.add_job(cid, "website", f"{name}{i}", f"Engineer {i}", loc, None, recent)
        ids[name] = cid
    db.update_company(ids["Site"], abroad_note="offshore development center")
    db.commit()
    s = score_all(db, load_desk(db))
    assert s[ids["Local"]].total > 0
    assert s[ids["Offshore"]].total == 0 and "outside the US" in s[ids["Offshore"]].reasons[0][1]
    assert s[ids["Site"]].total == 0 and "abroad" in s[ids["Site"]].reasons[0][1]
    assert [r["company"] for r in today.call_list(db)] == ["Local"]


def test_crawl_notes_an_offshore_team(tmp_path, site):
    from zoomollo.pipeline import crawl_company
    site["/"] = "<title>Acme</title><p>Our offshore development center in Pune works around the clock.</p>"
    db = DB(str(tmp_path / "c.db"))
    crawl_company(db, "acme.test", max_pages=2, delay=0)
    assert "offshore development" in db.conn.execute("SELECT abroad_note FROM companies").fetchone()[0]


class FakeLoxo:
    base = "https://loxo.test/api/x"

    def __init__(self):
        self.created, self.records = [], {}
        outer = self

        class S:
            def post(self, url, json=None, timeout=None):
                outer.created.append(json)
                pid = 900 + len(outer.created)
                outer.records[pid] = {"id": pid, "emails": [], "phones": []}

                class R:
                    status_code, content, text = 200, b"{}", ""

                    def raise_for_status(self):
                        pass

                    def json(self):
                        return {"person": {"id": pid}}
                return R()
        self.session = S()

    def get(self, endpoint, **params):
        if endpoint == "person_types":
            return {"person_types": [{"id": 7, "name": "Contact"}]}
        if endpoint == "people":
            return {"people": []}
        return self.records[int(endpoint.split("/")[1])]


def setup_person(tmp_path):
    db = DB(str(tmp_path / "l.db"))
    save_desk(db, "controller")
    cid = db.company("acme.test", domain="acme.test", name="Acme")
    db.update_company(cid, city="Dallas, TX", phone="+12145550100", last_crawled=date.today().isoformat())
    db.add_job(cid, "website", "1", "Controller", "Dallas, TX", None, (date.today() - timedelta(days=5)).isoformat())
    pid = db.add_person(cid, "Dana Example", "CFO", linkedin_url="https://www.linkedin.com/in/dana-example")
    db.commit()
    return db, cid, pid


def test_loxo_adds_prospects_and_copies_back_what_it_finds(tmp_path, monkeypatch):
    monkeypatch.setenv("LOXO_AUTO_ADD", "5")
    db, cid, pid = setup_person(tmp_path)
    loxo = FakeLoxo()
    r = loxoenrich.auto(db, client=loxo)
    assert r["added"] == 1 and loxo.created[0]["person"]["name"] == "Dana Example"
    assert loxo.created[0]["person"]["person_type_id"] == [7]
    lid = db.conn.execute("SELECT loxo_person_id FROM people WHERE id = ?", (pid,)).fetchone()[0]
    # Loxo's extension / agent finds details: a toll-free line, a support inbox, a mobile and a work email.
    loxo.records[lid].update(emails=[{"value": "info@acme.test"}, {"value": "dana@gmail.test"},
                                     {"value": "dana@acme.test"}],
                             phones=[{"value": "(800) 555-0100"}, {"value": "(214) 555-0177"}])
    db.conn.execute("UPDATE people SET loxo_pulled_at = '2000-01-01' WHERE id = ?", (pid,))
    r = loxoenrich.auto(db, client=loxo)
    p = db.conn.execute("SELECT * FROM people WHERE id = ?", (pid,)).fetchone()
    assert r["found"] == 1 and p["email"] == "dana@acme.test" and p["email_status"] == "found"
    assert p["phone"] == "+12145550177"
    assert loxoenrich.auto(db, client=loxo)["added"] == 0                       # never added twice


def test_daily_cap_and_off_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("LOXO_AUTO_ADD", "0")
    db, cid, pid = setup_person(tmp_path)
    assert loxoenrich.auto(db, client=FakeLoxo())["added"] == 0


def test_button_adds_then_opens_linkedin(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOOMOLLO_NO_BACKGROUND", "1")
    monkeypatch.setenv("LOXO_AGENCY_SLUG", "x")
    monkeypatch.setenv("LOXO_API_KEY", "k")
    from zoomollo import loxo as loxomod
    from zoomollo.web import create_app
    fake = FakeLoxo()
    monkeypatch.setattr(loxomod, "LoxoClient", lambda *a, **k: fake)
    db, cid, pid = setup_person(tmp_path)
    c = create_app(db.path).test_client()
    assert "Add to Loxo &amp; find contact info" in c.get("/").get_data(as_text=True)
    r = c.post(f"/person/{pid}/loxo")
    assert r.status_code == 302 and r.headers["Location"] == "https://www.linkedin.com/in/dana-example"
    assert fake.created and "In Loxo · find contact info" in c.get("/").get_data(as_text=True)


def test_every_number_and_email_and_more_people(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOOMOLLO_NO_BACKGROUND", "1")
    monkeypatch.setenv("LOXO_AUTO_ADD", "10")
    db, cid, pid = setup_person(tmp_path)
    extra = [db.add_person(cid, n, t) for n, t in (("Lee Sample", "Controller"), ("Pat Demo", "VP Human Resources"),
                                                   ("Kim Test", "Chief Executive Officer"),
                                                   ("Al Rep", "Customer Service Representative"))]
    db.commit()
    assert extra[3] is None                                                     # a CSR is never kept
    loxo = FakeLoxo()
    assert loxoenrich.auto(db, client=loxo)["added"] == 3                      # the card's person + two colleagues
    lid = db.conn.execute("SELECT loxo_person_id FROM people WHERE id = ?", (pid,)).fetchone()[0]
    loxo.records[lid].update(
        emails=[{"value": "dana@acme.test", "email_type": {"name": "Work"}}, {"value": "dana@gmail.test", "type": "personal"}],
        phones=[{"value": "(214) 555-0101", "phone_type": {"name": "Work"}},
                {"value": "(469) 555-0177", "phone_type": {"name": "Mobile"}}, {"value": "888-555-0100"}])
    got = loxoenrich.pull(db, pid, loxo)
    assert set(got) == {"dana@acme.test", "dana@gmail.test", "+12145550101", "+14695550177"}
    p = db.conn.execute("SELECT phone, email FROM people WHERE id = ?", (pid,)).fetchone()
    assert p["phone"] == "+14695550177" and p["email"] == "dana@acme.test"     # mobile first, work email first
    chans = loxoenrich.channels(db, pid)
    assert [c["value"] for c in chans][:2] == ["+14695550177", "dana@acme.test"] and len(chans) == 4
    assert {c["label"] for c in chans} == {"Mobile", "Work", "Personal"}
    monkeypatch.setenv("LOXO_AGENCY_SLUG", "x")
    monkeypatch.setenv("LOXO_API_KEY", "k")
    from zoomollo.web import create_app
    html = create_app(db.path).test_client().get("/").get_data(as_text=True)
    assert "(214) 555-0101" in html and "dana@gmail.test" in html and "personal" in html
    assert "2 other contacts at Acme" in html and "Pat Demo" in html and "Kim Test" in html and "Al Rep" not in html
