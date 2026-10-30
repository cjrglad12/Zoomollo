from zoomollo.db import DB
from zoomollo.extract import extract_page
from zoomollo.xref import cross_reference, hiring_rows, reports_to


def test_reports_to():
    assert reports_to("This role reports directly to the Service Manager and works with techs.") == ["Service Manager"]
    assert reports_to("Reporting to our VP of Sales, you will...") == ["VP of Sales"]


def test_cross_reference(tmp_path):
    db = DB(str(tmp_path / "x.db"))
    cid = db.company("acme.com", domain="acme.com", name="Acme")
    db.add_person(cid, "Owen Hart", "Owner")
    db.add_person(cid, "Sara Lane", "Service Manager", email="sara.lane@acme.com")
    db.add_person(cid, "Tom Ford", "Sales Director")
    db.add_person(cid, "Rae Kim", "Recruiter")
    db.add_contact(cid, "email", "careers@acme.com", "role")
    db.add_contact(cid, "email", "info@acme.com", "role")
    db.add_job(cid, "website", "1", "HVAC Technician", "Austin", "u1", None,
               "<p>Reports to the Service Manager. Questions? Text 512-555-0134.</p>")
    db.add_job(cid, "greenhouse", "2", "Account Executive", "Austin", "u2", None,
               "Send your resume to Tom Ford or hiring@acme.com")
    db.add_job(cid, "website", "3", "Bookkeeper", None, "u3", None, "")
    db.commit()
    cross_reference(db, cid)

    def matches(job_ext):
        jid = db.conn.execute("SELECT id FROM jobs WHERE external_id = ?", (job_ext,)).fetchone()[0]
        return [(r["reason"], r["name"] or r["value"]) for r in db.job_contacts(jid)]

    m1 = matches("1")
    assert m1[0] == ("reports to", "Sara Lane")
    assert ("contact in posting", "+15125550134") in m1
    assert not any(v in ("Rae Kim", "careers@acme.com", "info@acme.com") for _, v in m1)
    m2 = matches("2")
    assert m2 == [("named in posting", "Tom Ford")]  # hiring@ in the post is ignored
    assert matches("3")[0] == ("owner / exec", "Owen Hart")   # no finance leader known
    rows = hiring_rows(db, "technician")
    assert {r["job_title"] for r in rows} == {"HVAC Technician"}
    hf = db.conn.execute("SELECT hiring_for FROM people WHERE name = 'Sara Lane'").fetchone()[0]
    assert hf == "HVAC Technician"


def test_careers_page_jobs():
    html = """<script type="application/ld+json">{"@context":"https://schema.org","@type":"JobPosting",
      "title":"Commercial HVAC Installer","datePosted":"2026-10-01","identifier":{"value":"J-77"},
      "jobLocation":{"@type":"Place","address":{"addressLocality":"Austin","addressRegion":"TX"}},
      "description":"<p>Reports to the Install Manager</p>"}</script>
      <a href="/careers/service-technician">Service Technician</a>
      <a href="/careers/">View all jobs</a><a href="/about">Project Manager</a>
      <a href="/careers/apply">Apply now</a>"""
    r = extract_page(html, "https://acme.com/careers")
    assert [(j["title"], j["external_id"], j["location"]) for j in r.jobs] == [
        ("Commercial HVAC Installer", "J-77", "Austin, TX"),
        ("Service Technician", "https://acme.com/careers/service-technician", None)]
    assert extract_page(html.split("</script>")[1], "https://acme.com/services").jobs == []


def test_guess_seen_in_posting_becomes_found(tmp_path):
    from zoomollo import emailcheck
    db = DB(str(tmp_path / "g.db"))
    cid = db.company("acme.com", domain="acme.com", name="Acme")
    a = db.add_person(cid, "Ravi Shah", "VP of Sales")
    b = db.add_person(cid, "Amy Cole", "Controller")
    db.commit()
    emailcheck.guess_all(db, cid)  # ravi.shah@ / amy.cole@
    db.add_job(cid, "website", "1", "Account Executive", None, "u", None, "Email rshah@acme.com to apply")
    emailcheck.mark_bad(db, "ravi.shah@acme.com")
    emailcheck.mark_bad(db, "ravi@acme.com")
    assert db.conn.execute("SELECT email FROM people WHERE id = ?", (a,)).fetchone()[0] == "rshah@acme.com"
    cross_reference(db, cid)
    row = db.conn.execute("SELECT email, email_status FROM people WHERE id = ?", (a,)).fetchone()
    assert tuple(row) == ("rshah@acme.com", "found")
    # The proven format spreads to colleagues.
    assert db.conn.execute("SELECT email FROM people WHERE id = ?", (b,)).fetchone()[0] == "acole@acme.com"
