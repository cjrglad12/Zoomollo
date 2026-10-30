from zoomollo import pipeline
from zoomollo.db import DB

def test_crawl_end_to_end(site, tmp_path):
    db = DB(str(tmp_path / "t.db"))
    res = pipeline.crawl_company(db, "acme.test", max_pages=10, delay=0)
    assert res["emails"] == 2 and res["phones"] == 1  # hr@ is a generic inbox: never stored
    rows = {r["value"]: r for r in db.search()}
    assert rows["jane.doe@acme.test"]["name"] == "Jane Doe"
    assert "hr@acme.test" not in rows
    co = db.companies()[0]
    assert co["email_pattern"] == "{first}.{last}" and co["name"] == "Acme Inc"

    # Guess uses learned pattern; suppression removes and blocks.
    guesses = pipeline.guess_for_person(db, "Sam Lee", "acme.test", save=False)
    assert guesses[0] == "sam.lee@acme.test"
    assert db.suppress("jane.doe@acme.test") == 1
    assert not db.add_contact(1, "email", "jane.doe@acme.test", "person")
    db.suppress("@acme.test")
    assert not any(r["type"] == "email" for r in db.search())
    assert "hr@acme.test" not in pipeline.export_csv(db)


def test_web_ui(tmp_path):
    from zoomollo.web import create_app
    db = DB(str(tmp_path / "w.db"))
    cid = db.company("acme.com", domain="acme.com")
    db.add_contact(cid, "email", "jane@acme.com", "person", name="Jane")
    db.commit()
    c = create_app(db.path).test_client()
    assert b"Prospect" in c.get("/").data
    assert b"jane@acme.com" in c.get("/contacts").data
    assert c.get(f"/company/{cid}").status_code == 200
    assert c.get("/companies").status_code == 200
    assert c.get("/jobs").status_code == 200
    assert b"jane@acme.com" in c.get("/export.csv").data


def test_sitemap_finds_unlinked_pages(site, tmp_path):
    site["/robots.txt"] = "User-agent: *\nSitemap: https://acme.test/sm-index.xml"
    site["/sm-index.xml"] = "<sitemapindex><sitemap><loc>https://acme.test/sm-team.xml</loc></sitemap></sitemapindex>"
    site["/sm-team.xml"] = ("<urlset><url><loc>https://acme.test/team/carla-diaz-bio</loc></url>"
                            "<url><loc>https://acme.test/blog/x</loc></url></urlset>")
    site["/team/carla-diaz-bio"] = '<h2>Carla Diaz</h2><p>Chief Operating Officer</p>'
    db = DB(str(tmp_path / "s.db"))
    pipeline.crawl_company(db, "acme.test", max_pages=20, delay=0)
    people = {p["name"]: p for p in db.people()}
    assert people["Carla Diaz"]["seniority"] == "c_level"
