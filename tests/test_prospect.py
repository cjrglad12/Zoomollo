from zoomollo import discover, jobboards, pipeline
from zoomollo.db import DB
from zoomollo.search import Result, SearchProvider


class FakeSearch(SearchProvider):
    name = "fake"
    min_interval = 0

    def _search(self, q, count):
        if "site:linkedin.com/in" in q:
            return [Result("Pat Kim - Service Manager - Acme Inc | LinkedIn", "https://linkedin.com/in/patkim"),
                    Result("Lee Wong - Talent Acquisition Partner - Acme Inc | LinkedIn", "https://linkedin.com/in/lw"),
                    Result("Someone Else - CEO - Other Co | LinkedIn", "https://linkedin.com/in/other")]
        return [Result("Acme Inc | Commercial HVAC in Austin", "https://acme.test/"),
                Result("Top 10 HVAC Companies in Austin", "https://blog.example.net/top-10"),
                Result("Acme HVAC - Yelp", "https://www.yelp.com/biz/acme")]


def test_prospect_end_to_end(site, tmp_path, monkeypatch):
    site["/our-team"] = ('<h3>Jane Doe</h3><p>Owner & President</p><h3>Bob Smith</h3><p>Operations Director</p>'
                         '<p>Email bob.smith@acme.test. Office (512) 555-0134.</p>')
    site["/careers"] = '<a href="https://boards.greenhouse.io/acmehvac">Open roles</a>'

    monkeypatch.setattr(discover, "_http_get", lambda url, **kw: [{"boundingbox": ["30.1", "30.5", "-97.9", "-97.5"]}])
    monkeypatch.setattr(discover, "_http_post", lambda url, **kw: {"elements": [
        {"tags": {"name": "Acme Inc", "website": "https://www.acme.test", "phone": "+1 512 555 0100",
                  "craft": "hvac", "addr:city": "Austin"}},
        {"tags": {"name": "Cool Air LLC", "craft": "hvac"}},
    ]})
    monkeypatch.setattr(discover.time, "sleep", lambda s: None)
    monkeypatch.setattr(jobboards, "_get", lambda url, **p: {"jobs": [
        {"id": 7, "title": "HVAC Service Technician", "location": {"name": "Austin"}, "absolute_url": "u",
         "updated_at": "", "content": ""}]})
    monkeypatch.setattr(pipeline, "has_mx", lambda d: True)
    monkeypatch.setattr("zoomollo.crawler.Crawler.__init__.__defaults__", (40, 0, 15.0, "ZoomolloBot", "US"))

    db = DB(str(tmp_path / "p.db"))
    log = []
    res = pipeline.prospect(db, "commercial HVAC", "Austin, TX", limit=5, provider=FakeSearch(),
                            progress=log.append)
    assert res["companies"] == 2 and res["jobs"] == 1

    acme = next(c for c in db.companies() if c["key"] == "acme.test")
    assert acme["name"] == "Acme Inc" and acme["city"] == "Austin, TX" and acme["phone"] == "+1 512 555 0100"
    assert acme["email_pattern"] == "{first}.{last}"
    people = {p["name"]: p for p in db.people(acme["id"])}
    assert set(people) == {"Jane Doe", "Bob Smith", "Pat Kim", "Lee Wong"}
    assert people["Bob Smith"]["email"] == "bob.smith@acme.test" and people["Bob Smith"]["email_status"] == "found"
    assert people["Pat Kim"]["email"] == "pat.kim@acme.test" and people["Pat Kim"]["email_status"] == "guessed"
    # Technician role -> Service Manager (ops, closest level up) + recruiter
    assert people["Pat Kim"]["hiring_for"] == "HVAC Service Technician"
    assert people["Lee Wong"]["hiring_for"] == "HVAC Service Technician"
    assert people["Jane Doe"]["department"] == "executive"

    csv = pipeline.export_people_csv(db, hiring_only=True)
    assert "Pat Kim" in csv and "Jane Doe" not in csv
    # Directory sites and listicles are not companies.
    assert not any("yelp" in c["key"] or "example.net" in c["key"] for c in db.companies())
