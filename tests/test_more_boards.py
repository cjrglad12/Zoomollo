from types import SimpleNamespace

from zoomollo import jobboards
from zoomollo.extract import find_ats

G = "b1eed6f2-0da5-461f-f107-740df7173dfc"


def test_find_ats_new_systems():
    page = ('<script src="https://bartlett-cocke-general-contractors.breezy.hr/embed/js"></script>'
            f'<script src="https://careers-content.clearcompany.com/js/v1/career-site.js?siteId={G}"></script>'
            '<a href="https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?'
            'cid=b6b43a92-20cb-43da-9702-087962aaab60&amp;ccId=1">Jobs</a>'
            '<a href="https://senderoconsulting.applytojob.com/apply/jobs/">Careers</a>'
            '<img src="https://gallery-cdn.breezy.hr/logo.png">'
            '<a href="https://recruiting.paylocity.com/recruiting/jobs/All/c02c24e1-f4f6-47f7-8251-5d232677ec80">x</a>')
    assert find_ats(page) == {("breezy", "bartlett-cocke-general-contractors"), ("clearcompany", G),
                              ("adp", "b6b43a92-20cb-43da-9702-087962aaab60"), ("jazzhr", "senderoconsulting"),
                              ("paylocity", "c02c24e1-f4f6-47f7-8251-5d232677ec80")}


def test_clearcompany_uses_office_not_headquarters(monkeypatch):
    rows = [{"id": "a1", "positionTitle": "HOU Sr. Estimator", "officeName": "Houston", "location": "McKinney TX",
             "locations": [{"city": "McKinney", "subdivision": "TX"}], "postedDate": "2026-02-18T00:00:00",
             "applyLink": "https://pogue.clearcompany.com/careers/jobs/a1/apply", "salaryRangeLow": "110000",
             "salaryRangeHigh": "140000", "description": "<p>Estimate <b>commercial</b> jobs</p>"}]
    monkeypatch.setattr(jobboards, "_get", lambda url, **p: {"results": rows, "totalCount": 1})
    [j] = jobboards.clearcompany(G)
    assert (j.title, j.location) == ("HOU Sr. Estimator", "Houston, TX")
    assert j.description.startswith("Salary $110000–$140000. Estimate commercial jobs")
    try:
        jobboards.clearcompany("not-a-guid")
        raise AssertionError("bad slug accepted")
    except ValueError:
        pass


def test_adp_and_breezy(monkeypatch):
    adp = {"meta": {"totalNumber": 1}, "jobRequisitions": [
        {"itemID": "92_1", "requisitionTitle": "Project Manager - Energy", "postDate": "2026-10-27T10:00:00",
         "requisitionLocations": [{"address": {"cityName": "Dallas", "countrySubdivisionLevel1": {"codeValue": "TX"}}},
                                  {"address": {"cityName": "Austin", "countrySubdivisionLevel1": {"codeValue": "TX"}}}]}]}
    monkeypatch.setattr(jobboards, "_get", lambda url, **p: adp)
    [j] = jobboards.adp("b6b43a92-20cb-43da-9702-087962aaab60")
    assert (j.location, j.posted_at) == ("Dallas, TX; Austin, TX", "2026-10-27")
    breezy = [{"id": "d9", "name": "Project Engineer", "url": "u", "published_date": "2026-11-07",
               "location": {"name": "Austin, TX", "is_remote": False}, "locations": [{"name": "Austin, TX"}],
               "type": {"name": "Full-Time"}, "salary": "$90k - $110k"}]
    monkeypatch.setattr(jobboards, "_get", lambda url, **p: breezy)
    [j] = jobboards.breezy("bartlett-cocke")
    assert (j.title, j.location) == ("Project Engineer", "Austin, TX") and "$90k" in j.description


def test_one_person_bio_page():
    from zoomollo.extract import extract_page
    page = ('<html><body><a href="mailto:info@example-eng.com">Contact</a>'
            '<div class="hero"><h1 class="name">Jo Sample</h1><p class="title">Project Manager</p></div>'
            '<a class="email" href="mailto:jsample@example-eng.com"><svg></svg></a></body></html>')
    [p] = extract_page(page, "https://example-eng.com/people/jo-sample/").people
    assert (p.name, p.title, p.email) == ("Jo Sample", "Project Manager", "jsample@example-eng.com")
    # A normal page with a headline is not a person.
    assert extract_page('<h1>Our Services</h1><p>Director of things</p>', "https://x.com/").people == []


def test_field_chiefs_are_not_executives():
    from zoomollo.extract import extract_page
    from zoomollo.orgmap import seniority
    assert seniority("Survey Party Chief") == "manager" and seniority("Survey Chief of Parties | Associate") == "manager"
    assert seniority("Crew Chief") == "manager" and seniority("Chief of Staff") == "manager"
    for t in ("Chief Technology Officer", "Chief People Officer", "Deputy Chief Financial Officer", "Chief Estimator",
              "CEO", "President"):
        assert seniority(t) == "c_level", t
    # A title the site labels as the title is kept even without a familiar word in it.
    page = '<h1>Robin Tester</h1><p class="title">CAD Technician</p><a href="mailto:rtester@example-eng.com">Email</a>'
    [p] = extract_page(page, "https://example-eng.com/people/robin-tester/").people
    assert (p.title, p.email) == ("CAD Technician", "rtester@example-eng.com")


def test_headlines_and_buttons_are_not_people():
    from zoomollo.extract import looks_like_name, plausible_title
    assert not looks_like_name("Pay My Invoice")
    for real in ("Bill Weston", "Bill Van Asselt", "Pay Ling Tan"):   # "Bill" is a name, not billing
        assert looks_like_name(real), real
    assert not plausible_title("ExampleEng announces Sam Placeholder as new CEO")
    assert plausible_title("Senior Vice President - Structural Engineering | Principal")


def test_hiring_manager_pick_prefers_the_leader_of_that_work():
    from zoomollo.today import pick_person
    roles = ["Electrical Engineer"]
    bd = {"name": "Eddy Daniel", "title": "Client Development Manager", "seniority": "manager", "department": "sales",
          "hiring_for": "Electrical Engineer", "phone": "+12145550100", "email": "e@d.com", "email_status": "found"}
    svp = {"name": "Luke Esser", "title": "Senior Vice President - Structural Engineering", "seniority": "vp",
           "department": "engineering", "hiring_for": "Structural Project Engineer; Electrical Engineer", "phone": None,
           "email": "l@d.com", "email_status": "found"}
    assert pick_person([bd, svp], roles)[0]["name"] == "Luke Esser"


def test_bad_number_is_removed_and_stays_gone(tmp_path):
    from zoomollo import today
    from zoomollo.db import DB
    db = DB(str(tmp_path / "b.db"))
    cid = db.company("ziosk.com", domain="ziosk.com", name="Ziosk")
    db.add_contact(cid, "phone", "+18775854822", "company")
    db.add_contact(cid, "phone", "+12145804800", "company")
    db.commit()
    assert today._main_line(db, cid, None) == "+12145804800"            # local line before the toll-free one
    undo = today.log_outcome(db, cid, None, "bad_number", "just tech support")
    assert undo["bad_number"]["number"] == "+12145804800"
    assert today._main_line(db, cid, None) == "+18775854822"            # the next number takes over
    assert db.add_contact(cid, "phone", "+12145804800", "company") is False   # a re-read can't bring it back
    assert not db.conn.execute("SELECT 1 FROM pipeline WHERE company_id = ?", (cid,)).fetchone()  # not a touch
    today.undo_outcome(db, undo)
    assert today._main_line(db, cid, None) == "+12145804800"


def test_job_link_and_company_name_cleanup():
    from zoomollo.extract import best_company_name, clean_job_link_title as c
    assert c("Data Scientist More Details") == "Data Scientist"
    assert c("Software Engineer Software Engineer Full Time plano More Details") == "Software Engineer"
    assert c("Sr. AI Engineer Aezion Bengaluru, Karnataka Full Time Posted 4 weeks ago") == "Sr. AI Engineer Aezion Bengaluru, Karnataka"
    assert c("Financial Associate (Full Time)") == "Financial Associate (Full Time)"
    assert c("Controller") == "Controller" and c("Senior Tax Manager - Apply Now") == "Senior Tax Manager"
    assert best_company_name("tapcheck.com", "Careers at Tapcheck") == "Tapcheck"
    assert best_company_name("acme.com", None, "Welcome to Acme | Home") == "Acme"
    assert best_company_name("alkami.com", "Alkami Technology - Digital Banking Solutions") == "Alkami Technology"


def test_logging_a_call_from_the_page_needs_no_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOOMOLLO_NO_BACKGROUND", "1")
    from zoomollo.db import DB
    from zoomollo.web import create_app
    path = str(tmp_path / "w.db")
    db = DB(path)
    cid = db.company("acme.com", domain="acme.com", name="Acme")
    db.commit()
    c = create_app(path).test_client()
    form = {"company": cid, "person": "", "outcome": "voicemail", "note": "x"}
    r = c.post("/today/log", data=form, headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200 and r.json["company"] == "Acme" and r.json["prog"]["dials"] == 1
    assert c.post("/today/log", data=form).status_code == 302          # a plain form still works
    page = c.get("/", headers={"Accept-Encoding": "gzip"})
    assert page.headers.get("Content-Encoding") == "gzip"              # pages go out compressed


def test_customers_quoted_on_a_site_are_not_staff():
    from zoomollo.pipeline import quoted_customer_page as q
    for url in ("https://www.tapcheck.com/partner/connecteam", "https://x.com/white-papers/all-you-need",
                "https://x.com/customer-stories/acme", "https://x.com/resources/case-studies/acme/", "https://x.com/reviews"):
        assert q(url), url
    for url in ("https://x.com/about/leadership", "https://x.com/team", "https://x.com/people/jo-sample/",
                "https://x.com/partnership-team", "https://x.com/"):
        assert not q(url), url


def test_dfw_number_first_and_undo_puts_it_back_in_place(tmp_path):
    from zoomollo import today
    from zoomollo.blitz import say_number
    from zoomollo.db import DB
    db = DB(str(tmp_path / "n.db"))
    cid = db.company("ascentt.com", domain="ascentt.com", name="Ascentt")
    db.add_contact(cid, "phone", "+15107978371", "company")   # California office, found first
    db.add_contact(cid, "phone", "+14694028429", "company")
    db.add_contact(cid, "phone", "+12145550199", "company")
    db.commit()
    assert today._main_line(db, cid, None) == "+14694028429"     # the Dallas number, not the first one found
    undo = today.log_outcome(db, cid, None, "bad_number", "")
    assert today._main_line(db, cid, None) == "+12145550199"
    today.undo_outcome(db, undo)
    assert today._main_line(db, cid, None) == "+14694028429"     # back where it was, not at the end of the line
    assert say_number("5125550123") == "512-555-0123" and say_number("+1 (512) 555-0123") == "512-555-0123"
    assert say_number("") == ""


def test_phone_pages_have_call_buttons_and_folds(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOOMOLLO_NO_BACKGROUND", "1")
    from zoomollo.db import DB
    from zoomollo.web import create_app
    path = str(tmp_path / "p.db")
    db = DB(path)
    cid = db.company("acme.com", domain="acme.com", name="Acme")
    db.add_contact(cid, "phone", "+12145550100", "company")
    db.add_person(cid, "Ann Lee", "Director of Engineering", source="website")
    db.commit()
    c = create_app(path).test_client()
    page = c.get(f"/company/{cid}").get_data(as_text=True)
    assert 'class="btn callbtn" href="tel:+12145550100"' in page          # one tap to call from the company page
    assert '<details class="zm-fold" open><summary>Show all 1 people' in page
    assert 'class="zm-fold"' in c.get("/pipeline").get_data(as_text=True)  # settings fold away on a phone
    assert 'class="zm-phone"' in c.get("/blitz").get_data(as_text=True)    # Start button at the top on a phone


def test_jobvite_pages(monkeypatch):
    row = ('<td class="jv-job-list-name">\n <a href="/acme/job/o{0}">{1}</a>\n</td>'
           '<td class="jv-job-list-location">\n {2}\n </td>')
    pages = [row.format("A1", "Controller", "Dallas,\n Texas") + row.format("B2", "Tax Manager", "3 Locations"), ""]
    monkeypatch.setattr(jobboards.requests, "get", lambda url, params=None, **k: SimpleNamespace(
        text=pages[min(params["p"], 1)], raise_for_status=lambda: None))
    a, b = jobboards.jobvite("acme")
    assert (a.title, a.location, a.url) == ("Controller", "Dallas, Texas", "https://jobs.jobvite.com/acme/job/oA1")
    assert (b.title, b.location) == ("Tax Manager", None)
    assert ("jobvite", "acme") in find_ats('<a href="https://jobs.jobvite.com/acme/jobs">Careers</a>')


def test_jazzhr_and_paylocity_pages(monkeypatch):
    jazz = ('<tr id="row_job_1" class="x"><td><a class="job_title_link" href="/apply/jobs/details/kW1J?&">'
            'Senior Accountant</a><br /><span class="resumator_department">Finance</span></td>'
            '<td>\n Dallas, TX </td></tr>')
    pay = ('<script>window.pageData = {"Jobs": [{"JobId": 77, "JobTitle": "Superintendent", '
           '"LocationName": "Dallas, TX", "PublishedDate": "2026-11-01T00:00:00", "IsInternal": false},'
           '{"JobId": 78, "JobTitle": "Internal only", "IsInternal": true}]};\n</script>')
    pages = {"applytojob": jazz, "paylocity": pay}
    monkeypatch.setattr(jobboards.requests, "get", lambda url, **k: SimpleNamespace(
        text=next(v for k2, v in pages.items() if k2 in url), raise_for_status=lambda: None))
    [j] = jobboards.jazzhr("acme")
    assert (j.title, j.location, j.description) == ("Senior Accountant", "Dallas, TX", "Finance")
    [p] = jobboards.paylocity("c02c24e1-f4f6-47f7-8251-5d232677ec80")
    assert (p.title, p.location, p.external_id) == ("Superintendent", "Dallas, TX", "77")
