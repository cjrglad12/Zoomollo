"""Rules that came out of the data and phone audit of 2026-11-12."""
from datetime import date, timedelta

from zoomollo import autosend, candidates, pipeline, today
from zoomollo.db import DB
from zoomollo.extract import _job_links, looks_like_name, plausible_title
from zoomollo.hiringscore import load_desk, save_desk, score_all


def test_people_quoted_from_other_companies_are_not_staff():
    q = pipeline.quoted_outsider
    page = ('"It helped to increase conversions by 145%." Angie Lombardi Vice President of Marketing at Franklin Group '
            '"Ultimately, we wanted more." Our team: Kendall Pretzer Chief Executive Officer at Grace Hill since 2019. '
            'Jane Roe Partner Jane began her career at Deloitte before joining us.')
    assert q("Angie Lombardi", page, "Grace Hill", "gracehill.com")
    assert not q("Kendall Pretzer", page, "Grace Hill", "gracehill.com")   # "at Grace Hill" is this company
    assert not q("Jane Roe", page, "Grace Hill", "gracehill.com")          # a bio mentioning a past employer
    assert not q("Nobody Here", page, "Grace Hill", "gracehill.com")


def test_job_posts_and_articles_are_not_read_for_people_or_phones():
    assert pipeline.job_posting_page("https://tekninjas.com/jobs/program-manager-2/")
    assert pipeline.job_posting_page("https://x.com/careers/openings/salesforce-architect")
    assert not pipeline.job_posting_page("https://www.turnerconstruction.com/careers/experienced-professionals")
    assert not pipeline.job_posting_page("https://x.com/careers")
    assert pipeline.article_page("https://www.criticalstart.com/resources/cs-sa-26-0303-the-threat")
    assert pipeline.article_page("https://x.com/blog/hello") and not pipeline.article_page("https://x.com/contact-us")
    assert pipeline.people_page("https://sst.cpa/blog/team-categories/senior-managers/")
    assert not pipeline.people_page("https://www.plektonlabs.com/contact/")


def test_headings_are_not_names_and_sentences_are_not_titles():
    for junk in ("Job Summary", "United States", "Mutual Funds", "Dispatch Representative", "Program Leadership",
                 "Employee Surveys", "Solution Bundles", "Current Openings", "Vast Network"):
        assert not looks_like_name(junk), junk
    for real in ("Luke Esser", "Bill Weston", "Angie Lombardi", "Jacob Feinstein"):
        assert looks_like_name(real), real
    assert not plausible_title("Lead end-to-end management of CDX initiatives from conception through delivery.")
    assert not plausible_title("Client is seeking an experienced Program Manager to lead its customer […]")
    assert plausible_title("Managing Partner Legal / Head Legal Services M.E.")
    assert plausible_title("Director, Controller and Financial Planning & Anal...")
    assert plausible_title("Co-Founder & Chief Revenue Officer")


def test_the_number_shown_is_the_local_office_not_a_far_one_or_junk(tmp_path):
    db = DB(str(tmp_path / "p.db"))
    par = db.company("pariveda.com", domain="pariveda.com", name="Pariveda")
    db.add_contact(par, "phone", "+16474846600", "company", source_url="https://pariveda.com/locations/toronto/")
    db.add_contact(par, "phone", "+18443252729", "company", source_url="https://pariveda.com/news/new-coo/")
    cs = db.company("criticalstart.com", domain="criticalstart.com", name="Critical Start")
    db.add_contact(cs, "phone", "+17277950797", "company", source_url="https://criticalstart.com/resources/advisory-1")
    db.add_contact(cs, "phone", "+18776842077", "company", source_url="https://criticalstart.com/resources/press-release")
    dun = db.company("example-eng.com", domain="example-eng.com", name="ExampleEng")
    db.add_contact(dun, "phone", "+15125550101", "company", source_url="https://example-eng.com/locations/austin/")
    db.add_contact(dun, "phone", "+18175550102", "company", source_url="https://example-eng.com/locations/fort-worth/")
    db.commit()
    # Toll-free lines go to customer service: the Toronto office (flagged "not a DFW number") beats it.
    assert today._main_line(db, par, None) == "+16474846600"
    assert today._main_line(db, cs, None) is None               # toll-free, or out of a threat advisory: nothing to dial
    assert not today.dialable("+18443252729") and today.dialable("+18175550102")
    assert today._main_line(db, dun, None) == "+18175550102"    # the Fort Worth office
    assert today.line_note("+18443252729") == "toll-free" and today.line_note("+16474846600") == "not a DFW number"
    assert today.line_note("+18175550102") == "" and today.line_note("") == ""


def test_year_old_postings_score_lower_and_auto_send_needs_a_local_account(tmp_path):
    db = DB(str(tmp_path / "s.db"))
    old = (date.today() - timedelta(days=700)).isoformat()
    new = (date.today() - timedelta(days=40)).isoformat()
    ids = {}
    for name, posted, city, loc in (("Fresh", new, "Dallas, TX", "Dallas, TX"), ("Stale", old, "Dallas, TX", "Dallas, TX"),
                                    ("Nowhere", new, None, None)):
        cid = db.company(f"{name.lower()}.com", domain=f"{name.lower()}.com", name=name)
        if city:
            db.update_company(cid, city=city)
        db.add_job(cid, "website", name, "Controller", loc, "u", posted)
        ids[name] = cid
    db.commit()
    save_desk(db, "controller", "", "Dallas, TX", None)
    desk = load_desk(db)
    s = score_all(db, desk)
    assert s[ids["Stale"]].total < s[ids["Fresh"]].total
    assert any("over a year ago" in why for _, why in s[ids["Stale"]].reasons)
    assert any("hard to fill" in why for _, why in s[ids["Fresh"]].reasons)
    assert autosend.in_area(db, ids["Fresh"], desk)
    assert not autosend.in_area(db, ids["Nowhere"], desk)     # no city on the company, no location on the role


def test_search_puts_title_matches_first(tmp_path):
    db = DB(str(tmp_path / "c.db"))
    db.conn.execute("INSERT INTO candidates (source, name, title, resume_text, created_at) VALUES "
                    "('upload', 'Ana Analyst', 'Cyber Security Analyst', ?, 'x')",
                    ("domain controller " * 20,))
    db.conn.execute("INSERT INTO candidates (source, name, title, resume_text, created_at) VALUES "
                    "('upload', 'Cora Controller', 'Financial Controller', 'Controller at Acme.', 'x')")
    cid = db.company("acme.com", domain="acme.com", name="Controller Supply Co")
    db.add_person(cid, "Zed Young", "Office Manager", source="website")
    db.add_person(cid, "Amy Old", "Controller", source="website")
    db.add_job(cid, "website", "1", "Warehouse Lead", "Controller Park, TX", "u1", None)
    db.add_job(cid, "website", "2", "Assistant Controller", "Dallas, TX", "u2", None)
    db.commit()
    _, found = candidates.search(db, "controller")
    assert [r["c"]["name"] for r in found] == ["Cora Controller", "Ana Analyst"]
    assert db.people(q="controller")[0]["name"] == "Amy Old"          # title match before a company-name match
    assert db.jobs(q="controller")[0]["title"] == "Assistant Controller"


def test_email_talks_about_a_role_on_your_desk(tmp_path):
    from zoomollo import messaging
    db = DB(str(tmp_path / "m.db"))
    cid = db.company("mp.com", domain="mp.com", name="MultiPoint")
    db.update_company(cid, city="Dallas, TX")
    db.add_job(cid, "website", "1", "Administrative Assistant", "Irving, TX", "u1", None)
    db.add_job(cid, "website", "2", "Electrical Engineer", "Irving, TX", "u2", None)
    pid = db.add_person(cid, "Jessica Gemar", "Chief People Officer", source="website")
    pid = db.conn.execute("SELECT id FROM people WHERE company_id = ?", (cid,)).fetchone()[0]
    db.conn.execute("UPDATE people SET hiring_for = 'Administrative Assistant; Electrical Engineer' WHERE id = ?", (pid,))
    db.commit()
    save_desk(db, "electrical engineer", "", "Irving, TX", None)
    assert messaging.person_fields(db, pid)["role"] == "Electrical Engineer"


def test_overseas_numbers_are_never_the_main_line(tmp_path):
    db = DB(str(tmp_path / "o.db"))
    cid = db.company("it.com", domain="it.com", name="IT Co")
    db.add_contact(cid, "phone", "+601112579331", "company", source_url="https://it.com/contact-us/")
    db.commit()
    assert today._main_line(db, cid, None) is None


def test_cards_carry_a_one_line_company_summary(tmp_path, monkeypatch):
    from zoomollo.extract import tight_summary as t
    assert t("Welcome to Acme! Acme is the leading provider of payroll software for restaurants. Learn more.",
             "Acme") == "Provider of payroll software for restaurants"
    assert t("Ascentt builds production-ready AI and data solutions for manufacturing. Deploy secure platforms.",
             "Ascentt") == "Builds production-ready AI and data solutions for manufacturing"
    assert t("At Compass Datacenters, we build customizable, sustainable and reliable data centers.",
             "Compass Datacenters") == "Build customizable, sustainable and reliable data centers"
    long = t("MEP, fire protection, technology and commissioning engineering for healthcare, aviation, education "
             "and public facilities across Texas and Colorado and many more places besides.", "MultiPoint")
    assert len(long) <= 111 and long.endswith("…")
    assert t("Please enable JavaScript and cookies to continue.", "X") is None and t("", "X") is None
    # The line shows on the Today card with size and city.
    monkeypatch.setenv("ZOOMOLLO_NO_BACKGROUND", "1")
    from zoomollo.web import create_app
    path = str(tmp_path / "s.db")
    db = DB(path)
    cid = db.company("acme.com", domain="acme.com", name="Acme")
    db.update_company(cid, city="Dallas, TX", headcount=240, summary="Payroll software for restaurants",
                      summary_source="website")
    db.add_contact(cid, "phone", "+12145550100", "company")
    db.add_job(cid, "website", "1", "Controller", "Dallas, TX", "u", None)
    db.commit()
    save_desk(db, "controller", "", "Dallas, TX", None)
    c = create_app(path).test_client()
    page = c.get("/").get_data(as_text=True)
    assert "Payroll software for restaurants · about 240 people · Dallas, TX" in page
    c.post(f"/company/{cid}/summary", data={"summary": "  Restaurant payroll   tools "})
    row = DB(path).company_row(cid)
    assert (row["summary"], row["summary_source"]) == ("Restaurant payroll tools", "you")


def test_bad_number_in_a_blitz_keeps_the_card_and_shows_the_next_number(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOOMOLLO_NO_BACKGROUND", "1")
    from zoomollo import blitz
    from zoomollo.web import create_app
    path = str(tmp_path / "b.db")
    db = DB(path)
    for n, name in enumerate(("Acme", "Bolt")):
        cid = db.company(f"{name.lower()}.com", domain=f"{name.lower()}.com", name=name)
        db.update_company(cid, city="Dallas, TX")
        db.add_contact(cid, "phone", f"+1214555010{n}", "company", source_url="https://x.com/contact")
        db.add_contact(cid, "phone", f"+1469555020{n}", "company", source_url="https://x.com/contact")
        db.add_job(cid, "website", "1", "Controller", "Dallas, TX", "u", None)
    db.commit()
    save_desk(db, "controller", "", "Dallas, TX", None)
    c = create_app(path).test_client()
    c.post("/blitz/start", data={"source": "priority", "size": "5", "action": "start"})
    page = c.get("/blitz/run").get_data(as_text=True)
    assert "Bad number" in page and "(214) 555-010" in page
    first = blitz.load_session(DB(path))["items"][0]["company"]
    c.post("/blitz/log", data={"i": "0", "outcome": "bad_number", "note": "tech support"})
    s = blitz.load_session(DB(path))
    assert s["i"] == 0 and blitz.tally(s)["dials"] == 0           # same card, and it isn't counted as a dial
    page = c.get("/blitz/run").get_data(as_text=True)
    assert first in page and "(469) 555-020" in page and "(214) 555-010" not in page
    c.post("/blitz/undo")
    assert blitz.load_session(DB(path))["i"] == 0
    assert "(214) 555-010" in c.get("/blitz/run").get_data(as_text=True)   # the number is back
    c.post("/blitz/log", data={"i": "0", "outcome": "voicemail", "note": ""})
    assert blitz.load_session(DB(path))["i"] == 1


def test_named_leaders_from_apollo_show_on_cards(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOOMOLLO_NO_BACKGROUND", "1")
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    from zoomollo import leads
    from zoomollo.web import create_app
    path = str(tmp_path / "l.db")
    db = DB(path)
    cid = db.company("tapcheck.com", domain="tapcheck.com", name="Tapcheck")
    db.update_company(cid, city="Plano, TX")
    db.add_contact(cid, "phone", "+12145550100", "company", source_url="https://tapcheck.com/contact")
    db.add_job(cid, "website", "1", "Controller", "Plano, TX", "u", None)
    db.commit()
    save_desk(db, "controller", "", "Plano, TX", None)
    found = [{"id": "a1", "first_name": "Katherine", "last_name_obfuscated": "Pe***y", "title": "Director, Talent Acquisition"},
             {"id": "a2", "first_name": "Katie", "last_name_obfuscated": "Gi***n", "title": "VP of Marketing"},
             {"id": "a3", "first_name": "Alex", "last_name_obfuscated": "Go***y", "title": "CFO"},
             {"id": "a4", "first_name": "Dana", "last_name_obfuscated": "Ex***e", "title": "VP, Controller"}]
    assert leads.save(db, cid, found) == 3            # the recruiter is never saved
    db.commit()
    assert leads.best(db, cid, ["Controller"], "CFO")["first_name"] == "Dana"
    assert leads.find_for_company(db, cid) is None     # no key: no lookup, nothing breaks
    c = create_app(path).test_client()
    page = c.get("/").get_data(as_text=True)
    assert "Ask for <b>Dana, VP, Controller</b>" in page and "Hi Dana" in page
    assert "Leaders found in Apollo" in c.get(f"/company/{cid}").get_data(as_text=True)
    c.post("/blitz/start", data={"source": "priority", "size": "5", "action": "start"})
    run = c.get("/blitz/run").get_data(as_text=True)
    assert "ask for <b>Dana, VP, Controller</b>" in run and "put me through to Dana, your VP, Controller" in run
    # With a key, a new company's leaders are looked up when it is read (no network here).
    monkeypatch.setenv("APOLLO_API_KEY", "test")
    monkeypatch.setattr(leads, "search", lambda domain, timeout=20: [{"id": "b1", "first_name": "Ron", "title": "CEO"}])
    other = db.company("acme.com", domain="acme.com", name="Acme")
    db.commit()
    assert leads.find_for_company(db, other) == 1 and leads.find_for_company(db, other) == 0   # once a month


def test_foreign_language_copies_of_a_job_are_skipped():
    from bs4 import BeautifulSoup
    html = ('<a href="/blogs/jobs/data-scientist-3/">Data Scientist</a>'
            '<a href="/de/blogs/jobs/data-scientist-3/">Data Scientist</a>')
    jobs = _job_links(BeautifulSoup(html, "html.parser"), "https://www.ascentt.com/careers/")
    assert [j["url"] for j in jobs] == ["https://www.ascentt.com/blogs/jobs/data-scientist-3/"]


def test_reading_a_site_never_holds_the_database(tmp_path, monkeypatch):
    """While Zoomollo waits on a website, a call logged from the phone must be able to save."""
    db = DB(str(tmp_path / "l.db"))
    seen = []

    def crawl(self, domain, *a, **k):
        seen.append(("crawl", db.conn.in_transaction))
        return iter(())

    def boards(crawler, domain, *a, **k):
        seen.append(("boards", db.conn.in_transaction))
        return set()

    monkeypatch.setattr(pipeline.Crawler, "crawl", crawl)
    monkeypatch.setattr(pipeline, "find_job_boards", boards)
    monkeypatch.setattr(pipeline, "guess_people_emails", lambda d, c: seen.append(("dns", d.conn.in_transaction)) or 0)
    pipeline.crawl_company(db, "newco.com")
    assert seen == [("crawl", False), ("boards", False), ("dns", False)]
