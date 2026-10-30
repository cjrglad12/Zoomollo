from zoomollo.db import DB
from zoomollo.exclusions import is_excluded_email, is_recruiting_title, staffing_signal


def test_generic_and_staffing_emails():
    for e in ["info@acme.com", "careers@acme.com", "jobs.austin@acme.com", "hr-team@acme.com",
              "info2@acme.com", "sales@acme.com", "talent@acme.com", "jane@roberthalf.com"]:
        assert is_excluded_email(e), e
    for e in ["jane.doe@acme.com", "jdoe@acme.com", "ian.smith@acme.com"]:
        assert not is_excluded_email(e), e


def test_titles_and_companies():
    assert is_recruiting_title("Senior Technical Recruiter")
    assert is_recruiting_title("Talent Acquisition Partner")
    assert not is_recruiting_title("HR Director") and not is_recruiting_title("Service Manager")
    assert staffing_signal("Acme Staffing | Temp-to-hire jobs in Austin")
    assert staffing_signal("Smith & Co", "Executive search firm for CFOs")
    assert staffing_signal("Jobs", domain="www.roberthalf.com")
    assert not staffing_signal("Acme Mechanical | We're hiring HVAC techs!", "Commercial HVAC in Austin")


def test_db_rejects_and_blocks(tmp_path):
    db = DB(str(tmp_path / "x.db"))
    cid = db.company("acme.com", domain="acme.com", name="Acme")
    assert not db.add_contact(cid, "email", "careers@acme.com", "role")
    assert db.add_person(cid, "Rae Kim", "Recruiter") is None
    pid = db.add_person(cid, "Sam Lee", None, email="sam.lee@acme.com")
    db.add_contact(cid, "email", "sam.lee@acme.com", "person", name="Sam Lee")
    assert db.add_person(cid, "Sam Lee", "Talent Acquisition Lead") is None   # later learned: removed
    assert db.search() == []
    assert not db.add_contact(cid, "phone", "+15125550100", "person", name="X", title="Recruiter")
    assert not db.conn.execute("SELECT 1 FROM people WHERE id = ?", (pid,)).fetchone()
    s = db.company("staffco.com", domain="staffco.com", name="StaffCo")
    db.add_person(s, "Ann Bo", "Owner")
    db.block_company(s, "staffing")
    assert db.is_blocked("staffco.com") and not db.people(s)


def test_crawl_blocks_staffing_site(site, tmp_path):
    from zoomollo import pipeline
    site["/"] = '<title>Acme Staffing - Temp Agency</title><a href="/our-team">Team</a>'
    db = DB(str(tmp_path / "b.db"))
    r = pipeline.crawl_company(db, "acme.test", max_pages=10, delay=0)
    assert r["blocked"] and db.is_blocked("acme.test") and db.companies() == []
    assert pipeline.crawl_company(db, "acme.test", delay=0)["pages"] == 0   # never crawled again


def test_existing_data_is_purged_on_upgrade(tmp_path):
    path = str(tmp_path / "old.db")
    db = DB(path)
    cid = db.company("acme.com", domain="acme.com", name="Acme")
    db.conn.execute("INSERT INTO contacts (company_id, type, value, kind, first_seen, last_seen) "
                    "VALUES (?, 'email', 'info@acme.com', 'role', 'x', 'x')", (cid,))
    db.conn.execute("INSERT INTO people (company_id, name, name_key, title, first_seen) "
                    "VALUES (?, 'Rae Kim', 'rae kim', 'Recruiter', 'x')", (cid,))
    db.conn.execute("PRAGMA user_version = 0")
    db.conn.commit()
    db = DB(path)
    assert db.search() == [] and db.people() == []
