import json

from zoomollo import opendata, pipeline
from zoomollo.crawler import Crawler
from zoomollo.db import DB

CATALOG = {"results": [
    {"resource": {"id": "perm-1234", "name": "Issued Construction Permits",
                  "description": "Building, electrical, mechanical (HVAC) and plumbing permits",
                  "columns_field_name": ["permit_number", "work_class", "description", "contractor_company_name",
                                         "contractor_full_name", "contractor_phone", "original_city"]},
     "metadata": {"domain": "data.austin.test"}},
    {"resource": {"id": "lic-5678", "name": "Mechanical Contractor Licenses",
                  "description": "Licensed air conditioning contractors",
                  "columns_field_name": ["business_name", "license_type", "phone", "city", "state"]},
     "metadata": {"domain": "data.austin.test"}},
    {"resource": {"id": "viol-0001", "name": "Code Violations", "description": "contractor complaints",
                  "columns_field_name": ["business_name"]}, "metadata": {"domain": "data.austin.test"}},
    {"resource": {"id": "trees-01", "name": "Street Trees", "description": "",
                  "columns_field_name": ["species"]}, "metadata": {"domain": "data.austin.test"}},
]}

PERMITS = [
    {"permit_number": "1", "work_class": "Mechanical", "description": "Replace HVAC rooftop unit",
     "contractor_company_name": "ARCTIC AIR LLC", "contractor_full_name": "DANA WHITE",
     "contractor_phone": "5125550101", "original_city": "AUSTIN"},
    {"permit_number": "2", "work_class": "Mechanical", "description": "New HVAC ductwork",
     "contractor_company_name": "ARCTIC AIR LLC", "contractor_phone": "5125550101", "original_city": "AUSTIN"},
    {"permit_number": "3", "work_class": "Mechanical", "description": "HVAC split system",
     "contractor_company_name": "Lone Star Cooling", "contractor_phone": "5125550102", "original_city": "AUSTIN"},
    {"permit_number": "4", "work_class": "Plumbing", "description": "Water heater",
     "contractor_company_name": "Pipe Pros", "original_city": "AUSTIN"},
]
LICENSES = [
    {"business_name": "Lone Star Cooling", "license_type": "HVAC Contractor", "phone": "5125550102",
     "city": "Austin", "state": "TX"},
    {"business_name": "Hill Country HVAC", "license_type": "HVAC Contractor", "city": "Austin", "state": "TX"},
]


def fake_portal(site, monkeypatch):
    site["data.austin.test/api/catalog/v1"] = json.dumps(CATALOG)
    site["data.austin.test/resource/perm-1234.json"] = json.dumps(PERMITS)
    site["data.austin.test/resource/lic-5678.json"] = json.dumps(LICENSES)
    monkeypatch.setitem(opendata.CITY_PORTALS, ("austin", "TX"), ["data.austin.test"])
    monkeypatch.setitem(opendata.STATE_PORTALS, "TX", ["notaportal.test"])
    site["notaportal.test/api/catalog/v1"] = "<html>not json</html>"
    monkeypatch.setattr(opendata, "guessed_portals", lambda city, state: [])


def test_portals_and_guessing():
    assert opendata.portals_for("Austin, TX", guess=False)[0] == "data.austintexas.gov"
    assert "data.texas.gov" in opendata.portals_for("Austin, TX", guess=False)
    assert "data.cityofboise.org" in opendata.guessed_portals("Boise", "ID")


def test_finds_license_and_permit_datasets(site, monkeypatch):
    fake_portal(site, monkeypatch)
    picked = opendata.pick_datasets("data.austin.test", "commercial HVAC")
    assert [d["id"] for d in picked][:2] == ["lic-5678", "perm-1234"]          # no violations, no trees
    assert opendata.pick_datasets("notaportal.test", "hvac") == []
    found, notes = opendata.find_public_records("commercial HVAC", "Austin, TX",
                                                reachable=lambda d: d.endswith(".test"))
    by_name = {c.name: c for c in found}
    assert set(by_name) == {"Arctic Air Llc", "Lone Star Cooling", "Hill Country HVAC"}    # no plumber
    assert by_name["Arctic Air Llc"].contact_name == "Dana White"
    assert any("2 records" in s for s in by_name["Arctic Air Llc"].sources)       # most active first
    # Licensed AND permitted first, then the most permits, then the rest.
    assert [c.name for c in found] == ["Lone Star Cooling", "Arctic Air Llc", "Hill Country HVAC"]


def test_prospect_uses_public_records(site, tmp_path, monkeypatch):
    fake_portal(site, monkeypatch)
    from zoomollo import discover
    monkeypatch.setattr(discover, "_resolves", lambda host: host.endswith(".test"))
    db = DB(str(tmp_path / "r.db"))
    res = pipeline.prospect(db, "HVAC", "Austin, TX", limit=5, use_osm=False, use_hubs=False,
                            crawler=Crawler(delay=0))
    names = {c["name"] for c in db.companies()}
    assert {"Arctic Air Llc", "Lone Star Cooling"} <= names and res["companies"] == 3
    arctic = next(c for c in db.companies() if c["name"] == "Arctic Air Llc")
    assert [p["name"] for p in db.people(arctic["id"])] == ["Dana White"]
