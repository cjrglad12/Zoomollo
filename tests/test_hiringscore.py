from datetime import date, timedelta

from zoomollo import crm, hiringscore
from zoomollo.db import DB
from zoomollo.hiringscore import job_fit, parse_salary, save_desk, score_all


def ago(days):
    return (date.today() - timedelta(days=days)).isoformat()


def setup(tmp_path):
    db = DB(str(tmp_path / "h.db"))
    save_desk(db, "HVAC technician, service manager", "intern", "Austin, TX", "55k")

    def company(key, name, jobs, crawled=True, recruiter=False, people=()):
        cid = db.company(key, domain=key, name=name)
        db.update_company(cid, city="Austin, TX", last_crawled=ago(1) if crawled else None)
        for i, (title, posted, desc) in enumerate(jobs):
            db.add_job(cid, "website", f"{key}-{i}", title, "Austin, TX", None, posted, desc)
        if recruiter:
            db.add_person(cid, "Rae Kim", "Technical Recruiter")
        for name_, title, email, status, phone, hiring in people:
            pid = db.add_person(cid, name_, title, email=email, email_status=status, phone=phone)
            db.conn.execute("UPDATE people SET hiring_for = ? WHERE id = ?", (hiring, pid))
        db.commit()
        return cid

    ids = {
        "hot": company("hot.com", "Hot HVAC", [("HVAC Service Technician", ago(45), "Pay $28-$34/hr"),
                                              ("Service Manager", ago(3), ""), ("Office Admin", ago(3), "")],
                       people=[("Pat", "Operations Director", "pat@hot.com", "found", None,
                                "HVAC Service Technician; Service Manager")]),
        "recruiter": company("big.com", "Big Corp", [("HVAC Technician", ago(5), "")], recruiter=True),
        "offniche": company("acct.com", "Acct Co", [("Accountant", ago(5), ""), ("Bookkeeper", ago(5), "")]),
        "cheap": company("cheap.com", "Cheap Co", [("HVAC Technician", ago(5), "$40,000 per year")]),
        "intern": company("intern.com", "Intern Co", [("HVAC Technician Intern", ago(5), "")]),
        "none": company("none.com", "No Jobs", []),
    }
    return db, ids


def test_salary_and_fit():
    assert parse_salary("$28-$34/hr") == 34 * 2080 and parse_salary("up to $95k") == 95000
    assert parse_salary("401k and $5,000 bonus") is None              # bonus too small to be a salary
    d = hiringscore.Desk(["HVAC technician"], [], ["MO"], None)
    assert job_fit("HVAC Technician", "St. Louis, MO", None, None, d)[0]
    assert job_fit("HVAC Technician", "Kansas City, Missouri", None, None, d)[0]
    assert not job_fit("HVAC Technician", "Austin, TX", None, None, d)[0]


def test_scores_rank_the_right_companies(tmp_path):
    db, ids = setup(tmp_path)
    s = score_all(db)
    hot = s[ids["hot"]]
    assert hot.fit_roles == ["HVAC Service Technician", "Service Manager"] and hot.other_roles == 1
    reasons = dict((r, p) for p, r in hot.reasons)
    assert reasons["2 roles in your niche: HVAC Service Technician, Service Manager"] == 35
    assert reasons["1 open 30+ days (hard to fill)"] == 10
    assert reasons["no in-house recruiter found"] == 10 and reasons["hiring manager reachable"] == 10
    assert hot.total == 35 + 10 + 2 + 10 + 10
    assert s[ids["recruiter"]].total == 25 - 10 + 0                   # has a recruiter
    assert s[ids["offniche"]].fit_roles == [] and s[ids["offniche"]].total < 10
    assert s[ids["cheap"]].fit_roles == [] and s[ids["intern"]].fit_roles == []
    assert s[ids["none"]].total == 0
    order = [co["name"] for sc, co in hiringscore.ranked(db)]
    assert order[0] == "Hot HVAC" and order.index("Big Corp") < order.index("Acct Co")
    assert [co["name"] for _, co in hiringscore.ranked(db, niche_only=True)] == ["Hot HVAC", "Big Corp"]


def test_stale_roles_and_offlimits_and_pipeline(tmp_path):
    db, ids = setup(tmp_path)
    db.conn.execute("UPDATE jobs SET last_seen = ? WHERE company_id = ?", (ago(60), ids["recruiter"]))
    db.commit()
    assert score_all(db)[ids["recruiter"]].fit_roles == []           # not listed for 60 days: gone
    from zoomollo.offlimits import set_manual
    set_manual(db, ids["hot"], "partner's client")
    s = score_all(db)[ids["hot"]]
    assert s.total == 0 and s.offlimits.blocked
    set_manual(db, ids["hot"], None)
    crm.add_to_pipeline(db, ids["hot"])
    best = hiringscore.ranked(db, exclude_pipeline=True)
    assert "Hot HVAC" not in [co["name"] for _, co in best]


def test_web_desk_and_ranking(tmp_path):
    from zoomollo.web import create_app
    db, ids = setup(tmp_path)
    c = create_app(db.path).test_client()
    page = c.get("/companies").data.decode()
    assert page.index("Hot HVAC") < page.index("Acct Co") and "hard to fill" in page
    c.post("/desk", data={"titles": "accountant", "exclude": "", "locations": "", "min_salary": ""})
    page = c.get("/companies?niche=1").data.decode()
    assert "Acct Co" in page and "Hot HVAC" not in page
    jobs = c.get("/jobs").data.decode()
    assert "fits my desk" in jobs and "not a role you place" in jobs
    assert "Hiring score" in c.get(f"/company/{ids['offniche']}").data.decode()
    assert "Best accounts to work next" in c.get("/pipeline").data.decode()
