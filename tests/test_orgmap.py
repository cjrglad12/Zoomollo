from zoomollo.extract import extract_page, find_ats
from zoomollo.orgmap import department, hiring_managers, seniority


def test_titles():
    assert (seniority("VP of Sales"), department("VP of Sales")) == ("vp", "sales")
    assert (seniority("Owner"), department("Owner")) == ("c_level", "executive")
    assert seniority("Vice President, Operations") == "vp"
    assert seniority("Partner Manager") == "manager"
    assert seniority("Product Owner") == "staff" and department("Product Owner") == "product"
    assert (seniority("Talent Acquisition Manager"), department("Talent Acquisition Manager")) == ("manager", "hr")
    assert department("HVAC Service Technician") == "operations"
    assert department("Senior Software Engineer") == "engineering"


def test_hiring_managers():
    P = lambda i, n, t: {"id": i, "name": n, "title": t, "department": department(t), "seniority": seniority(t)}  # noqa
    people = [P(1, "Ann", "CEO"), P(2, "Ben", "VP of Sales"), P(3, "Cy", "Sales Manager"),
              P(4, "Di", "Recruiter"), P(5, "Ed", "Controller")]
    jobs = [{"id": 10, "title": "Account Executive"}, {"id": 11, "title": "Sales Director"},
            {"id": 12, "title": "HVAC Technician"}]
    hm = hiring_managers(people, jobs)
    assert [p["name"] for p in hm[10]] == ["Cy", "Ben"]
    assert [p["name"] for p in hm[11]] == ["Ben"]
    assert [p["name"] for p in hm[12]] == ["Ann"]  # no ops leader known -> owner/CEO; never the recruiter


def test_team_cards_and_ats():
    html = """<div class="team"><div><h3>Maria Lopez</h3><p>Director of Operations</p></div>
    <div><h3>Tom Reed</h3><p>Chief Financial Officer</p></div><div><h3>Our Services</h3><p>We fix things</p></div></div>
    <div><h3>Ann Lee</h3><p>Controller</p><a href="https://www.linkedin.com/in/annlee/">in</a></div>
    <a href="https://boards.greenhouse.io/acmehvac/jobs/123">Jobs</a>
    <script src="https://boards.greenhouse.io/embed/job_board/js?for=acme2"></script>"""
    r = extract_page(html, "https://acme.com/team")
    assert {(p.name, p.title, p.linkedin) for p in r.people} == {
        ("Maria Lopez", "Director of Operations", None), ("Tom Reed", "Chief Financial Officer", None),
        ("Ann Lee", "Controller", "https://www.linkedin.com/in/annlee/")}  # only her own card's link
    assert r.ats == {("greenhouse", "acmehvac"), ("greenhouse", "acme2")}
    assert find_ats("https://jobs.lever.co/acme/abc") == {("lever", "acme")}
