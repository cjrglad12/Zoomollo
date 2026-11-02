from zoomollo import candidates as cands, crm
from zoomollo.db import DB
from zoomollo.offlimits import set_manual

TAX_RESUME = ("Jane Smith\nMilwaukee, WI\nSenior Tax Manager (2016 - present)\nReviewed partnership returns.\n"
              "Manager, Tax (2012-2016)")
AUDIT_RESUME = "Bob Jones\nDallas, TX\nAudit Senior (2018 - present)\nPrepared workpapers."


class FakeLoxo:
    def __init__(self):
        self.calls = []

    def get(self, endpoint, **params):
        self.calls.append(endpoint)
        if endpoint == "people":
            if "scroll_id" not in params:
                return {"people": [
                    {"id": 1, "name": "Jane Smith", "current_title": "Tax Manager", "current_company": "Baker Tilly",
                     "city": "Milwaukee", "state": "WI", "emails": [{"value": "jane@ex.org"}],
                     "phones": [{"value": "+14145550199"}], "person_types": [{"name": "Candidate"}],
                     "skillsets": [{"name": "Partnership tax"}], "updated_at": "2026-10-01"},
                    {"id": 2, "name": "Carl Client", "current_title": "CFO", "person_types": [{"name": "Contact"}]},
                    {"id": 3, "name": "Bob Jones", "current_title": "Audit Senior", "state": "TX",
                     "person_types": [{"name": "Candidate"}, {"name": "Contact"}], "updated_at": "2026-09-01"},
                ], "scroll_id": "A"}
            return {"people": []}
        if endpoint == "people/1":
            return {"resumes": [{"name": "jane.txt", "download_url": "https://files.loxo.test/jane.txt",
                                 "updated_at": "2026-10-01"}],
                    "job_profiles": [{"title": "Tax Manager", "company_name": "Baker Tilly", "start_date": "2016-01-01"}]}
        if endpoint == "people/3":
            return {"resumes": []}
        raise AssertionError(endpoint)


def test_loxo_sync_and_resume_download(site, tmp_path):
    site["files.loxo.test/jane.txt"] = TAX_RESUME
    db = DB(str(tmp_path / "c.db"))
    fake = FakeLoxo()
    assert cands.sync_loxo(db, client=fake) == {"people": 3, "candidates": 2}       # client contact skipped
    jane = db.conn.execute("SELECT * FROM candidates WHERE loxo_id = 1").fetchone()
    assert (jane["title"], jane["email"], jane["state"], jane["skills"]) == ("Tax Manager", "jane@ex.org", "WI", "Partnership tax")
    assert cands.resume_backlog(db) == 2
    groups, results = cands.search(db, "tax manager, Wisconsin")                     # profile data already searchable
    assert [r["c"]["name"] for r in results] == ["Jane Smith"]
    assert list(cands.RESUME_QUEUE)[:1] == [jane["id"]]                               # search result jumps the queue
    assert cands.fill_resumes(db, limit=5, client=fake) == 2
    jane = db.conn.execute("SELECT * FROM candidates WHERE loxo_id = 1").fetchone()
    assert jane["resume_status"] == "fetched" and "Reviewed partnership returns" in jane["resume_text"]
    assert "Baker Tilly 2016-01-01" in jane["profile"]
    bob = db.conn.execute("SELECT resume_status FROM candidates WHERE loxo_id = 3").fetchone()
    assert bob[0] == "none"


def setup(tmp_path):
    db = DB(str(tmp_path / "s.db"))
    a = cands.add_upload(db, "jane_smith.txt", TAX_RESUME.encode())
    b = cands.add_upload(db, "bob.txt", AUDIT_RESUME.encode())
    co = db.company("acme.com", domain="acme.com", name="Acme CPAs")
    db.update_company(co, city="Austin, TX")
    db.add_job(co, "website", "1", "Tax Manager", "Austin, TX", "u", None)
    db.add_job(co, "website", "2", "Receptionist", "Austin, TX", "u", None)
    hm = db.add_person(co, "Ann Lee", "Tax Director", email="ann.lee@acme.com", email_status="found")
    db.commit()
    from zoomollo.xref import cross_reference
    cross_reference(db, co)
    return db, a, b, co, hm


def test_upload_search_rank_and_snippets(tmp_path):
    db, a, b, co, hm = setup(tmp_path)
    jane = db.conn.execute("SELECT * FROM candidates WHERE id = ?", (a,)).fetchone()
    assert (jane["name"], jane["state"], jane["title"]) == ("Jane Smith", "WI", "Senior Tax Manager")
    groups, results = cands.search(db, "tax manager with experience reviewing who lives in Texas or Wisconsin")
    assert [r["c"]["name"] for r in results] == ["Jane Smith", "Bob Jones"]          # Bob only matches Texas
    assert results[0]["fit"] == "strong fit" and results[1]["fit"] == ""
    assert results[1]["match"].hits[:2] == [0, 0] and results[1]["match"].hits[2] >= 1


def test_matching_jobs_and_anonymous_pitch(tmp_path):
    db, a, b, co, hm = setup(tmp_path)
    db.conn.execute("UPDATE candidates SET title = 'Senior Tax Manager' WHERE id = ?", (a,))
    db.commit()
    jobs = cands.matching_jobs(db, a)
    assert [r["job"]["title"] for r in jobs] == ["Tax Manager"] and jobs[0]["manager"]["name"] == "Ann Lee"
    p = cands.pitch(db, a, jobs[0]["job"]["id"])
    assert p["to"] == "ann.lee@acme.com" and p["subject"] == "Senior Tax Manager for your Tax Manager opening"
    assert "Hi Ann," in p["body"] and "about 14 years' experience" in p["body"] and "based in Milwaukee, WI" in p["body"]
    assert "Jane" not in p["body"] and "Baker" not in p["body"]                         # anonymised
    set_manual(db, co, "partner's client")
    assert cands.matching_jobs(db, a)[0]["offlimits"].blocked


def test_candidate_pages(tmp_path):
    from zoomollo.web import create_app
    db, a, b, co, hm = setup(tmp_path)
    db.conn.execute("UPDATE candidates SET title = 'Senior Tax Manager' WHERE id = ?", (a,))
    db.commit()
    c = create_app(db.path).test_client()
    page = c.get("/candidates?q=tax manager with experience reviewing who lives in Texas or Wisconsin").data.decode()
    assert page.index("Jane Smith") < page.index("Bob Jones") and "strong fit" in page
    assert 'class="kchip k0"' in page and 'class="kchip miss"' in page                 # Bob's missing keywords grey
    one = c.get(f"/candidate/{a}?q=tax manager, review, Wisconsin | WI").data.decode()
    assert '<mark class="k0"' in one and '<mark class="k1"' in one and '<mark class="k2"' in one
    assert "Companies hiring for this" in one and "Acme CPAs" in one
    job = db.conn.execute("SELECT id FROM jobs WHERE title = 'Tax Manager'").fetchone()[0]
    pitch = c.get(f"/pitch?candidate={a}&job={job}").data.decode()
    assert "ann.lee@acme.com" in pitch and "Senior Tax Manager for your Tax Manager opening" in pitch
    r = c.post("/pitch", data={"candidate": a, "job": job, "to": "ann.lee@acme.com", "subject": "S", "body": "B",
                               "action": "mailto"})
    assert r.headers["Location"].startswith("mailto:ann.lee@acme.com")
    assert crm.card(db, co)["stage"] == "contacted"
    r = c.post("/candidates/upload", data={"files": [(__import__("io").BytesIO(b"Sam Lee\nDenver, CO\nTax Analyst"), "sam.txt")]},
               content_type="multipart/form-data")
    assert "Added 1" in c.get(r.headers["Location"]).data.decode()
    assert "Find candidates" in c.get("/jobs").data.decode()
