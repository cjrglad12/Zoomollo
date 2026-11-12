from zoomollo import hiringscore
from zoomollo.db import DB
from zoomollo.extract import extract_page, find_headcount
from zoomollo.hiringscore import Desk, load_desk, save_desk, score_all, wrong_size


def test_headcount_from_a_companys_own_words():
    assert find_headcount("We are a team of 300+ engineers and planners.") == 300
    assert find_headcount("Founded in 1985, the firm has more than 250 employees across Texas.") == 250
    assert find_headcount("Our 1,200 team members serve clients nationwide") == 1200
    assert find_headcount("a 450-person firm headquartered in Dallas") == 450
    assert find_headcount("", '{"@type":"Organization","numberOfEmployees":{"@type":"QuantitativeValue","value":180}}') == 180
    # Not headcount: years, client counts, years in business.
    assert find_headcount("Serving Dallas since 1985 with over 40 years of experience and 500 projects.") is None
    assert find_headcount("Founded in 2024. Trusted by 3,000 customers.") is None
    page = extract_page("<html><body><p>Today our team of 220 professionals works in five offices.</p></body></html>",
                        "https://x.com/about")
    assert page.headcount == 220


def test_size_range_is_ish():
    desk = Desk(min_headcount=100, max_headcount=500)
    assert wrong_size(None, desk) is None                       # unknown: kept
    assert wrong_size(80, desk) is None and wrong_size(600, desk) is None   # a little outside still counts
    assert "too small" in wrong_size(40, desk)
    assert "too large" in wrong_size(2500, desk) and "100–500" in wrong_size(2500, desk)
    assert wrong_size(5000, Desk()) is None                     # no range set


def test_score_drops_companies_outside_the_range(tmp_path):
    db = DB(str(tmp_path / "h.db"))
    ids = {}
    for name, n in (("Right", 250), ("Tiny", 12), ("Giant", 9000), ("Unknown", None)):
        cid = db.company(f"{name.lower()}.com", domain=f"{name.lower()}.com", name=name)
        db.update_company(cid, city="Dallas, TX", headcount=n)
        db.add_job(cid, "website", name, "Controller", "Dallas, TX", "u", None)
        ids[name] = cid
    db.commit()
    save_desk(db, "controller", "", "Dallas, TX", None, None, "100", "500")
    desk = load_desk(db)
    assert (desk.min_headcount, desk.max_headcount) == (100, 500)
    s = score_all(db, desk)
    assert s[ids["Right"]].total > 0 and s[ids["Unknown"]].total > 0
    assert s[ids["Tiny"]].total == 0 and "too small" in s[ids["Tiny"]].reasons[0][1]
    assert s[ids["Giant"]].total == 0 and "too large" in s[ids["Giant"]].reasons[0][1]
    assert any("headcount not known" in r for _, r in s[ids["Unknown"]].reasons)
    # Saving the desk from a form that doesn't show the size fields keeps them.
    save_desk(db, "controller", "", "Dallas, TX", None)
    assert load_desk(db).max_headcount == 500
