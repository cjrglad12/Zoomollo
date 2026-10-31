"""End-to-end prospect run against fake websites (no network)."""

import json

from zoomollo import discover, jobboards, pipeline
from zoomollo.crawler import Crawler
from zoomollo.db import DB

CHAMBER = "austinchamber.test"


def fake_map(url, **kw):
    q = kw["data"]["data"]
    if "chamber" in q.lower():  # business groups query
        return {"elements": [{"tags": {"name": "Greater Austin Chamber of Commerce",
                                       "website": f"https://{CHAMBER}", "office": "association"}}]}
    return {"elements": [
        {"tags": {"name": "Acme Inc", "website": "https://www.acme.test", "phone": "+1 512 555 0100",
                  "craft": "hvac", "addr:city": "Austin"}},
        {"tags": {"name": "Cool Air LLC", "craft": "hvac", "phone": "(512) 555-0177"}},  # no website listed
        {"tags": {"name": "Temp Pros Staffing", "office": "employment_agency"}},
    ]}


def build_web(site):
    # Company found on the map
    site["/"] = '<title>Acme Inc - Commercial HVAC in Austin, TX</title><a href="/our-team">Team</a>'
    site["/our-team"] = ('<h3>Jane Doe</h3><p>Owner & President</p><h3>Bob Smith</h3><p>Operations Director</p>'
                         '<h3>Pat Kim</h3><p>Service Manager</p><h3>Lee Wong</h3><p>Talent Acquisition Partner</p>'
                         '<p>Email bob.smith@acme.test or info@acme.test. Office (512) 555-0134.</p>'
                         '<a href="https://partnerduct.test/">Our duct partner</a>')
    site["/careers"] = '<a href="https://boards.greenhouse.io/acmehvac">Open roles</a>'
    # Chamber member directory (on a subdomain, as many are)
    site[f"{CHAMBER}/"] = '<a href="https://business.austinchamber.test/list">Member Directory</a>'
    site[f"business.{CHAMBER}/list"] = ('<a href="/list/ql/heating-air-conditioning-22">Heating & Air Conditioning</a>'
                                        '<a href="/list/ql/restaurants-5">Restaurants</a>')
    site[f"business.{CHAMBER}/list/ql/heating-air-conditioning-22"] = (
        '<a href="https://www.frostyhvac.test/">Frosty HVAC Services</a>'
        '<a href="https://bigstaff.test/">Big Staff Recruiting</a>')
    site[f"business.{CHAMBER}/list/ql/restaurants-5"] = '<a href="https://tacoplace.test/">Visit Website</a>'
    site["frostyhvac.test/"] = '<title>Frosty HVAC | Heating & cooling for Austin businesses</title>'
    site["bigstaff.test/"] = '<title>Big Staff | Staffing agency for HVAC techs</title>'
    site["tacoplace.test/"] = '<title>Taco Place</title><p>Best tacos in Austin</p>'
    # Linked from Acme's site
    site["partnerduct.test/"] = '<title>Partner Duct</title><p>Commercial HVAC ductwork across Texas</p>'
    # Map listing without a website: guessed from its name, confirmed by the phone number
    site["coolair.test/"] = '<title>Cool Air</title><p>Call (512) 555-0177 for AC repair</p>'


def test_prospect_without_any_search_service(site, tmp_path, monkeypatch):
    build_web(site)
    monkeypatch.setattr(discover, "_http_get", lambda url, **kw: [{"boundingbox": ["30.1", "30.5", "-97.9", "-97.5"]}])
    monkeypatch.setattr(discover, "_http_post", fake_map)
    monkeypatch.setattr(discover.time, "sleep", lambda s: None)
    discover.geocode.cache_clear()
    # Website guessing: only our fake hosts "exist"; .test stands in for .com.
    monkeypatch.setattr(discover, "_resolves", lambda host: host == "coolair.com")
    real_fetch = Crawler.fetch
    monkeypatch.setattr(Crawler, "fetch", lambda self, url: real_fetch(self, url.replace("coolair.com", "coolair.test")))
    monkeypatch.setattr(jobboards, "_get", lambda url, **p: {"jobs": [
        {"id": 7, "title": "HVAC Service Technician", "location": {"name": "Austin"}, "absolute_url": "u",
         "updated_at": "", "content": "Reports to the Service Manager."}]})
    monkeypatch.setattr(pipeline, "has_mx", lambda d: True)
    monkeypatch.setattr("zoomollo.crawler.Crawler.__init__.__defaults__", (40, 0, 15.0, "ZoomolloBot", "US"))

    db = DB(str(tmp_path / "p.db"))
    log = []
    res = pipeline.prospect(db, "commercial HVAC", "Austin, TX", limit=10, crawler=Crawler(delay=0),
                            progress=log.append)
    keys = {c["key"] for c in db.companies()}
    # Map listing, chamber member, linked partner, guessed website; never staffing or the taco place.
    assert {"acme.test", "frostyhvac.test", "partnerduct.test", "coolair.com"} <= keys, (keys, log)
    assert not keys & {"bigstaff.test", "tacoplace.test"} and not any("temp" in k for k in keys)
    assert res["found_via_links"] == 1 and res["jobs"] == 1

    acme = next(c for c in db.companies() if c["key"] == "acme.test")
    people = {p["name"]: p for p in db.people(acme["id"])}
    assert set(people) == {"Jane Doe", "Bob Smith", "Pat Kim"}          # recruiter excluded
    assert people["Bob Smith"]["email"] == "bob.smith@acme.test"
    assert people["Pat Kim"]["email"] == "pat.kim@acme.test" and people["Pat Kim"]["email_status"] == "guessed"
    assert people["Pat Kim"]["hiring_for"] == "HVAC Service Technician"
    assert not db.conn.execute("SELECT 1 FROM contacts WHERE value LIKE 'info@%'").fetchone()


def test_public_data_source(site, tmp_path, monkeypatch):
    rows = [
        {"contractor_name": "SUMMIT MECHANICAL LLC", "license_type": "Air Conditioning and Refrigeration",
         "phone": "512-555-0190", "city": "Austin", "state": "TX", "owner_name": "MARIA LOPEZ"},
        {"contractor_name": "Hill Country Plumbing", "license_type": "Plumbing", "city": "Austin", "state": "TX"},
        {"contractor_name": "Dallas Air Co", "license_type": "Air Conditioning", "city": "Dallas", "state": "TX"},
    ]
    site["data.austin.test/resource/abcd-1234.json"] = json.dumps(rows)
    from zoomollo.datasources import from_source
    found = from_source("https://data.austin.test/resource/abcd-1234.json", "HVAC air conditioning", "Austin, TX")
    assert [(c.name, c.phone, c.contact_name) for c in found] == [
        ("Summit Mechanical Llc", "512-555-0190", "Maria Lopez")]

    csv_file = tmp_path / "licenses.csv"
    csv_file.write_text("Business Name,License Type,City,Phone\nAcme Air,HVAC Contractor,Austin,5125550111\n"
                        "Zed Roofing,Roofing,Austin,5125550122\n")
    assert [c.name for c in from_source(str(csv_file), "hvac", "Austin, TX")] == ["Acme Air"]

    db = DB(str(tmp_path / "s.db"))
    db.add_source(str(csv_file), "Austin licenses")
    monkeypatch.setattr(discover, "_resolves", lambda host: False)
    res = pipeline.prospect(db, "hvac", "Austin, TX", limit=5, use_osm=False, use_hubs=False,
                            crawler=Crawler(delay=0))
    co = db.companies()[0]
    assert res["companies"] == 1 and co["name"] == "Acme Air" and co["phone"] == "5125550111"
