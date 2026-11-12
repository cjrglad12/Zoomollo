from zoomollo import pipeline
from zoomollo.crawler import normalize_domain
from zoomollo.db import DB
from zoomollo.exclusions import works_elsewhere


def test_normalize_domain_drops_ports_and_logins():
    assert normalize_domain("anbtx.com:443") == "anbtx.com"
    assert normalize_domain("https://www.Acme.com:8443/about") == "acme.com"
    assert normalize_domain("http://user@acme.com/") == "acme.com"
    assert normalize_domain("acme.com") == "acme.com"


def test_works_elsewhere():
    # Customers / partners quoted on alkami.com
    for t in ["Chief Growth Officer, Ideal Credit Union",
              "Director of Digital Marketing & Analytics, Metritrust Credit Union",
              "VP & Retail Sales Manager, INB, National Association",
              "CFO at First Texas Bank",
              "Board member, University of North Texas",
              "President, Bank of Clarke"]:
        assert works_elsewhere(t, "Alkami Technology", "alkami.com"), t
    # Real staff titles, including ones with commas and org-sounding departments
    for t in ["Partner, Audit - Financial Services", "Principal, Consulting", "Director, Credit Union Partnerships",
              "Chief Executive Officer", "President, Alkami Technology Inc", "VP, Commercial Banking",
              "Managing Director, Consulting", "Tax Market Managing Principal", None, ""]:
        assert not works_elsewhere(t, "Alkami Technology", "alkami.com"), t
    assert not works_elsewhere("President, First Bank", "First Bank", "firstbank.com")


def test_quoted_customers_not_saved_and_cleaned(tmp_path):
    db = DB(str(tmp_path / "x.db"))
    cid = db.company("alkami.com", domain="alkami.com", name="Alkami")
    assert db.add_person(cid, "Steve Zich", "Chief Marketing Officer, Capital Credit Union", source="website") is None
    assert db.add_person(cid, "Alex Shootman", "Chief Executive Officer", source="website")
    # A typed-in contact is always kept
    assert db.add_person(cid, "Pat Lee", "CFO, Acme Bank", source="manual")
    # Rows saved before the check existed get removed on repair
    db.conn.execute("INSERT INTO people (company_id, name, name_key, title, source, first_seen) VALUES "
                    "(?, 'Old Quote', 'old quote', 'CEO, Ideal Credit Union', 'website', '')", (cid,))
    assert db.fix_saved_data()["people"] == 1
    names = {p["name"] for p in db.people(cid)}
    assert names == {"Alex Shootman", "Pat Lee"}


def test_port_in_saved_domain_repaired(tmp_path):
    db = DB(str(tmp_path / "x.db"))
    cid = db.company("anbtx.com:443", domain="anbtx.com:443")
    assert db.fix_saved_data()["domains"] == 1
    row = db.company_row(cid)
    assert row["domain"] == "anbtx.com" and row["key"] == "anbtx.com"


def test_set_website(tmp_path):
    db = DB(str(tmp_path / "x.db"))
    cid = db.company("name:walker-engineering", name="Walker Engineering")
    assert db.set_website(cid, "https://www.walkerengineering.com/") == cid
    assert db.company_row(cid)["domain"] == "walkerengineering.com"
    # Crawling the site later finds this same company, not a new one
    assert db.company("walkerengineering.com", domain="walkerengineering.com") == cid

    # Website already belongs to another company: the pipeline card moves there
    other = db.company("turner.com", domain="turner.com", name="Turner")
    dup = db.company("name:turner-construction", name="Turner Construction Co.")
    db.conn.execute("INSERT INTO pipeline (company_id, stage, created_at, updated_at) VALUES (?, 'prospect', '', '')",
                    (dup,))
    assert db.set_website(dup, "turner.com") == other
    assert db.company_row(dup) is None
    assert db.conn.execute("SELECT company_id FROM pipeline").fetchone()[0] == other


def test_fill_missing_guesses(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "has_mx", lambda d: True)
    db = DB(str(tmp_path / "x.db"))
    cid = db.company("acme.com", domain="acme.com", name="Acme")
    db.add_person(cid, "Jane Doe", "VP Operations", source="website")
    db.add_person(cid, "Bob Smith", "Service Manager", source="website", email="bob.smith@acme.com")
    nodomain = db.company("name:x", name="X")
    db.add_person(nodomain, "Al Green", "Owner", source="records")
    db.commit()
    assert pipeline.fill_missing_guesses(db) == 1
    jane = db.conn.execute("SELECT email, email_status FROM people WHERE name = 'Jane Doe'").fetchone()
    assert tuple(jane) == ("jane.doe@acme.com", "guessed")
    assert pipeline.fill_missing_guesses(db) == 0  # nothing left to do


def test_review_widgets_and_vendors_are_not_people():
    from zoomollo.extract import looks_like_name
    for junk in ["Google User", "SAP Ariba", "Project Management", "Yelp Reviewer", "Verified Customer"]:
        assert not looks_like_name(junk, "Acme"), junk
    for real in ["Jane Doe", "BRYAN WALDROP", "Mary Ann Smith", "John Miscavage CPA"]:
        assert looks_like_name(real, "Acme"), real
    for junk in ["Vendor Performance", "Frequently Asked Questions", "AICPA Member", "Community Involvement"]:
        assert not looks_like_name(junk, "Acme"), junk
    assert not looks_like_name("SDO CPA", "SDO CPA")  # the firm itself
    from zoomollo.extract import plausible_title
    assert plausible_title("Executive Vice President, Clinical Programs and CNO, Dallas Market")
    assert plausible_title(None)
    for copy in [": Tracking vendor lead times.", "What is a fractional CFO?", "Limits (e.g., travel)"]:
        assert not plausible_title(copy), copy


def test_email_wording():
    from zoomollo import messaging
    assert messaging.render("hiring an {role}", {"role": "Data Scientist"})[0] == "hiring a Data Scientist"
    assert messaging.render("hiring a {role}", {"role": "Engineer"})[0] == "hiring an Engineer"
    assert messaging.render("an SDET, an hour", {})[0] == "an SDET, an hour"


def test_email_role_is_one_you_place(tmp_path):
    from zoomollo import hiringscore, messaging
    db = DB(str(tmp_path / "x.db"))
    hiringscore.save_desk(db, titles="tax director", locations="Fort Worth, TX")
    cid = db.company("solera.com", domain="solera.com", name="Solera")
    db.update_company(cid, city="Fort Worth, TX")
    db.add_job(cid, "site", "1", "International Tax Director (Hybrid)", "Fort Worth, TX", None, None)
    db.add_job(cid, "site", "2", "Sr iOS Developer", "Fort Worth, TX", None, None)  # newer, not on the desk
    db.commit()
    assert messaging.person_fields(db, company_id=cid)["role"] == "International Tax Director"


def test_more_job_boards_spotted():
    from zoomollo.extract import find_ats
    page = ('<a href="https://ebqb.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/BDOExperiencedCareers">'
            '<a href="https://recruiting2.ultipro.com/WEA1003WEAV/JobBoard/6cc904ea-2cf8-4e29-8d2e-f318837bd1db/?q=">'
            '<a href="https://careers-criticalstart.icims.com/?in_iframe=1"> https://cdn02.icims.com/x.js '
            'https://c-12515-www-criticalstart-com.i.icims.com https://www.icims.com')
    assert find_ats(page) == {
        ("oracle", "ebqb.fa.us2.oraclecloud.com/BDOExperiencedCareers"),
        ("ultipro", "recruiting2.ultipro.com/WEA1003WEAV/6cc904ea-2cf8-4e29-8d2e-f318837bd1db"),
        ("icims", "careers-criticalstart.icims.com"),
    }


def test_icims_reader(monkeypatch):
    from zoomollo import jobboards
    card = ('<li class="iCIMS_JobCardItem"><span class="sr-only field-label">Job Locations</span> <span > '
            'US-TX-Dallas | US-TX-Austin</span><a href="https://careers-acme.icims.com/jobs/17/tax-manager/job'
            '?in_iframe=1" class="iCIMS_Anchor"><span>Title</span> <h3 > Tax Manager</h3></a>'
            '<div class="col-xs-12 description">Lead our tax team.</div></li>')
    pages = iter([card, ""])

    class R:
        def __init__(self, t): self.text = t
        def raise_for_status(self): pass
    monkeypatch.setattr(jobboards.requests, "get", lambda *a, **k: R(next(pages)))
    jobs = jobboards.icims("careers-acme.icims.com")
    assert [(j.title, j.location, j.external_id) for j in jobs] == \
        [("Tax Manager", "Dallas, TX; Austin, TX", "careers-acme.icims.com:17")]


def test_large_companies_skipped(tmp_path):
    from zoomollo import hiringscore
    db = DB(str(tmp_path / "x.db"))
    hiringscore.save_desk(db, titles="tax manager", locations="Dallas, TX", max_open_roles="5")
    big = db.company("bigfirm.com", domain="bigfirm.com", name="Big Firm")
    small = db.company("smallfirm.com", domain="smallfirm.com", name="Small Firm")
    kpmg = db.company("kpmg.com", domain="kpmg.com", name="KPMG")
    for cid in (big, small, kpmg):
        db.update_company(cid, city="Dallas, TX")
        db.add_job(cid, "site", f"{cid}-tax", "Tax Manager", "Dallas, TX", None, None)
    for i in range(6):
        db.add_job(big, "site", f"b{i}", f"Analyst {i}", "Dallas, TX", None, None)
    db.commit()
    s = hiringscore.score_all(db)
    assert s[small].total > 0
    assert s[big].total == 0 and "too large" in s[big].reasons[0][1]
    assert s[kpmg].total == 0 and "large national firm" in s[kpmg].reasons[0][1]
    # Saving the desk from somewhere that doesn't show the limit keeps it
    hiringscore.save_desk(db, titles="tax manager, audit", locations="Dallas, TX")
    assert hiringscore.load_desk(db).max_open_roles == 5


def test_contact_not_from_another_specialty():
    from zoomollo.today import pick_person
    people = [
        {"name": "Matt Reiter", "title": "Partner-in-Charge of Audit", "department": "finance", "seniority": "c_level",
         "hiring_for": None, "email": "m@x.com", "email_status": "guessed", "phone": None},
        {"name": "Brian Collins", "title": "Chief People Officer", "department": "hr", "seniority": "c_level",
         "hiring_for": None, "email": "b@x.com", "email_status": "guessed", "phone": None},
    ]
    person, why = pick_person(people, ["Tax Manager", "Tax Senior Manager"])
    assert person["name"] == "Brian Collins"
    person, _ = pick_person(people, ["Audit Senior Associate"])
    assert person["name"] == "Matt Reiter"


def test_salary_floor_and_residential():
    from zoomollo.hiringscore import Desk, job_fit
    desk = Desk(titles=["tax associate", "tax manager", "software engineer", "construction manager"],
                locations=["Dallas, TX"], min_salary=80000)
    ok = lambda t, pay=None: job_fit(t, "Dallas, TX", "Dallas, TX", pay, desk)[0]
    assert ok("Tax Manager") and ok("Tax Senior Associate") and ok("Software Engineer 2")
    assert not ok("State and Local Tax Associate")             # entry-level, no pay stated
    assert not ok("Tax Associate - Summer 2027 (Dallas)")
    assert not ok("Software Engineer 1")
    assert ok("Tax Associate", pay=95000)                     # stated pay wins
    assert not ok("Tax Manager", pay=70000)
    assert not ok("Residential Construction Manager")
    assert ok("Construction Manager")


def test_opens_and_outlook_sent_copies(tmp_path, monkeypatch):
    from email.message import EmailMessage
    from zoomollo import crm
    monkeypatch.setenv("MY_EMAILS", "me@outlook-firm.com, me@gmail.com")
    db = DB(str(tmp_path / "x.db"))
    cid = db.company("acme.com", domain="acme.com", name="Acme")
    pid = db.add_person(cid, "Jane Doe", "Tax Director", email="jane.doe@acme.com", source="website")
    db.commit()

    def msg(frm, to, subject, body, mid):
        m = EmailMessage()
        m["From"], m["To"], m["Subject"], m["Message-ID"] = frm, to, subject, mid
        m.set_content(body)
        return m.as_bytes()

    # A copy of an email you sent from Outlook is logged as outreach to Jane
    assert crm.process_message(db, msg("me@outlook-firm.com", "jane.doe@acme.com", "Tax Manager search",
                                       "Hi Jane", "<s1@x>")) == "sent"
    assert db.conn.execute("SELECT method FROM emails").fetchone()[0] == "outlook"
    # Outlook read receipt (sent by the reader)
    assert crm.process_message(db, msg("Jane Doe <jane.doe@acme.com>", "me@outlook-firm.com",
                                       "Read: Tax Manager search", "Your message was read on Tuesday.", "<r1@x>")) == "opened"
    # Tracking-tool notification, forwarded, naming her
    assert crm.process_message(db, msg("me@outlook-firm.com", "me@gmail.com", "FW: Jane Doe opened your email",
                                       "Jane Doe (jane.doe@acme.com) opened Tax Manager search", "<t1@x>")) == "opened"
    rows = db.conn.execute("SELECT person_id, subject FROM email_opens ORDER BY id").fetchall()
    assert [r[0] for r in rows] == [pid, pid] and rows[0][1] == "Tax Manager search"
    assert crm.process_message(db, msg("Jane Doe <jane.doe@acme.com>", "me@outlook-firm.com",
                                       "Read: Tax Manager search", "x", "<r1@x>")) is None  # same notification: once
    # An open from someone you don't know is ignored
    assert crm.process_message(db, msg("bob@other.com", "me@outlook-firm.com", "Read: hello", "x", "<r2@x>")) is None
    # A real reply from Jane is still a reply
    assert crm.process_message(db, msg("jane.doe@acme.com", "me@outlook-firm.com", "RE: Tax Manager search",
                                       "Sounds good, call me", "<p1@x>")) == "reply"


def test_ask_for():
    from zoomollo.today import ask_for
    # Most of the openings decide it, not the first keyword anywhere
    assert ask_for(["Senior Indirect Tax Consultant", "Sr. Accountant", "Senior Accountant",
                    "Healthcare Data Analyst"]) == "Controller"
    # A director-level opening goes one level up
    assert ask_for(["International Tax Director"]) == "VP of Tax / CFO"
    assert ask_for(["Tax Manager"]) == "Tax Director"
    assert ask_for(["Controller"]) == "CFO"
    assert ask_for(["Senior Data Engineer", "DevOps Engineer"]) == "Director of Data Engineering"
    assert ask_for(["Forklift Driver"]) == "hiring manager"
    assert ask_for(["Senior Software Engineer, Data & Cloud"]) == "Director of Software Engineering"
    # A CPA firm (tax + audit openings): partners own the hire, and one director role doesn't bump it up
    assert ask_for(["Tax Senior, Core Tax", "Tax Manager", "Tax Managing Director, SALT", "Assurance Senior"]) \
        == "Tax Partner"
    assert ask_for(["Audit Senior", "Audit Manager", "Tax Senior"]) == "Audit Partner"
    assert ask_for(["Field Engineer", "Construction Manager"]) == "Director of Construction"


def test_guesses_stay_guessed_after_learning_pattern(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "has_mx", lambda d: True)
    db = DB(str(tmp_path / "x.db"))
    cid = db.company("acme.com", domain="acme.com", name="Acme")
    db.add_person(cid, "Jane Doe", "VP Operations", source="website")
    db.commit()
    pipeline.guess_people_emails(db, cid)
    pipeline.learn_pattern(db, cid)
    status = db.conn.execute("SELECT email_status FROM people WHERE name = 'Jane Doe'").fetchone()[0]
    assert status == "guessed"
    # Rows mislabelled before the fix are repaired
    db.conn.execute("UPDATE people SET email_status = 'found'")
    assert db.fix_guess_labels() == 1
