from datetime import date, timedelta

from zoomollo import crm, pipeline, recheck
from zoomollo.db import DB
from zoomollo.hiringscore import save_desk, score_all


def careers(*roles):
    return "".join(f'<a href="/careers/{slug}">{title}</a>' for title, slug in roles)


def kinds(db):
    return [r[0] for r in db.conn.execute("SELECT kind FROM events ORDER BY id")]


def crawl(db):
    return pipeline.crawl_company(db, "acme.test", max_pages=20, delay=0)


def test_change_tracking_lifecycle(site, tmp_path):
    site["/"] = '<title>Acme Inc</title><a href="/careers">Careers</a><a href="/our-team">Team</a>'
    site["/our-team"] = "<h3>Jane Doe</h3><p>Owner</p>"
    site["/careers"] = careers(("HVAC Technician", "hvac-tech"), ("Office Administrator", "office-admin"))
    db = DB(str(tmp_path / "c.db"))
    save_desk(db, "HVAC technician, service manager, office administrator")

    crawl(db)                                                     # first look = baseline
    assert kinds(db) == [] and db.company_row(1)["last_checked"]
    cid = db.company_row(1)["id"]
    crm.add_to_pipeline(db, cid)

    site["/careers"] = careers(("HVAC Technician", "hvac-tech"), ("Service Manager", "svc-mgr"))
    site["/our-team"] = "<h3>Jane Doe</h3><p>Owner</p><h3>Bob Smith</h3><p>Operations Director</p>"
    crawl(db)
    assert sorted(kinds(db)) == ["job_new", "person_new"]
    admin = db.conn.execute("SELECT * FROM jobs WHERE title = 'Office Administrator'").fetchone()
    assert admin["missed_checks"] == 1 and admin["closed_at"] is None          # one miss isn't enough

    crawl(db)
    assert kinds(db)[-1] == "job_gone"
    assert "Office Administrator" not in [j["title"] for j in db.jobs(company_id=cid)]   # closed roles hidden

    site["/careers"] = careers(("HVAC Technician", "hvac-tech"), ("Service Manager", "svc-mgr"),
                               ("Office Administrator", "office-admin-2"))
    crawl(db)
    assert kinds(db)[-1] == "job_reposted"
    events = db.conn.execute("SELECT detail FROM events WHERE kind = 'job_reposted'").fetchone()
    assert "Office Administrator reposted" in events[0]
    # Changes land in the pipeline timeline too.
    assert sum(1 for a in crm.timeline(db, cid) if a["kind"] == "change") == 4

    s = score_all(db)[cid]
    why = " ".join(r for _, r in s.reasons)
    assert "reposted: Office Administrator" in why and "new leader" in why

    # Site down: nothing gets closed.
    for k in list(site):
        site.pop(k)
    r = crawl(db)
    assert r["pages"] == 0 and db.conn.execute("SELECT MAX(missed_checks) FROM jobs WHERE closed_at IS NULL").fetchone()[0] == 0


def test_due_and_run(site, tmp_path):
    site["/"] = '<title>Acme Inc</title><a href="/careers">Careers</a>'
    site["/careers"] = careers(("HVAC Technician", "hvac-tech"))
    db = DB(str(tmp_path / "d.db"))
    crawl(db)
    other = db.company("nosite.test", domain="nosite.test", name="No Site")
    db.commit()
    assert [c["domain"] for c in recheck.due(db, days=7)] == ["nosite.test"]   # acme checked today
    old = (date.today() - timedelta(days=8)).isoformat()
    db.update_company(1, last_checked=old)
    db.commit()
    crm.add_to_pipeline(db, 1)
    assert [c["domain"] for c in recheck.due(db, days=7)][0] == "acme.test"     # pipeline first
    site["/careers"] = careers(("HVAC Technician", "hvac-tech"), ("Estimator", "estimator"))
    r = recheck.run(db, limit=5, delay=0)
    assert r["checked"] == 1 and r["job_new"] == 1                              # nosite.test didn't load
    assert db.company_row(other)["last_checked"] is None


def test_changes_page(site, tmp_path):
    from zoomollo.web import create_app
    site["/"] = '<title>Acme Inc</title><a href="/careers">Careers</a>'
    site["/careers"] = careers(("HVAC Technician", "hvac-tech"))
    db = DB(str(tmp_path / "w.db"))
    save_desk(db, "HVAC technician")
    crawl(db)
    site["/careers"] = careers(("HVAC Technician", "hvac-tech"), ("Service Manager", "svc"))
    crawl(db)
    c = create_app(db.path).test_client()
    page = c.get("/changes").data.decode()
    assert "New roles" in page and "New role: Service Manager" in page
    assert "New role: Service Manager" not in c.get("/changes?niche=1").data.decode()   # not in my niche
    crm.add_to_pipeline(db, 1)
    db.add_event(1, "job_new", "New role: Test Role")
    db.commit()
    assert "Changes at your pipeline companies" in c.get("/pipeline").data.decode()
