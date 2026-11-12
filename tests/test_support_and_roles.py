from datetime import date, timedelta

from zoomollo import today
from zoomollo.db import DB
from zoomollo.exclusions import is_support_page, is_support_title
from zoomollo.extract import extract_page, find_support_phones
from zoomollo.hiringscore import save_desk


def test_customer_service_numbers_are_spotted():
    assert find_support_phones("Customer Service: (972) 555-0100 Main Office: (214) 555-0199") == {"+19725550100"}
    assert find_support_phones("Need help? Tech Support 24/7 at 972-555-0123") == {"+19725550123"}
    assert find_support_phones("For billing questions call 469-555-0111") == {"+14695550111"}
    # An address label or a plain office number is not support.
    assert not find_support_phones("Billing Address: 1 Main St, Dallas TX. Phone (214) 555-0199")
    assert not find_support_phones("Dallas office (214) 555-0199 · Fort Worth (817) 555-0102")
    page = extract_page('<p><a href="tel:2145550100">Call support</a></p><p>Office 214-555-0111</p>', "https://x.test/")
    assert page.support_phones == {"+12145550100"}
    assert extract_page("<p>Call 214-555-0100</p>", "https://x.test/support/contact").support_phones == {"+12145550100"}
    assert is_support_page("https://help.x.test/") and is_support_page("https://x.test/customer-service")
    assert not is_support_page("https://x.test/contact-us") and not is_support_page("https://x.test/helpful-guide")


def test_toll_free_is_not_a_working_number():
    assert not today.dialable("+18005550100") and not today.dialable("(888) 555-0100")
    assert today.dialable("+12145550100") and not today.dialable("")


def test_csrs_and_support_reps_are_never_kept():
    for t in ("Customer Service Representative", "Tech Support Specialist", "IT Help Desk Technician",
              "Client Services Associate", "Customer Success Manager", "Billing Specialist", "CSR"):
        assert is_support_title(t), t
    for t in ("Customer Service Manager", "Director of Customer Experience", "Controller", "VP Finance",
              "Senior Accountant", None):
        assert not is_support_title(t), t


def test_crawl_drops_support_numbers_and_people(tmp_path, site):
    from zoomollo.pipeline import crawl_company
    site["/"] = ('<title>Acme</title><a href="/contact-us">Contact</a><a href="/our-team">Team</a>'
                 '<p>Customer Support: (972) 555-0177</p><p>Main office: (214) 555-0198</p>')
    site["/contact-us"] = "<p>Headquarters (214) 555-0198</p>"
    site["/our-team"] = ('<div class="team-member"><h3>Jane Doe</h3><p>Controller</p></div>'
                         '<div class="team-member"><h3>Bob Smith</h3><p>Customer Service Representative</p></div>')
    db = DB(str(tmp_path / "c.db"))
    cid = db.company("acme.test", domain="acme.test", name="Acme")
    crawl_company(db, "acme.test", max_pages=5, delay=0)
    phones = {r[0] for r in db.conn.execute("SELECT value FROM contacts WHERE type = 'phone'")}
    assert "+19725550177" not in phones and "+12145550198" in phones
    assert db.is_suppressed("+19725550177")                       # never comes back from another page
    names = {r[0] for r in db.conn.execute("SELECT name FROM people")}
    assert "Bob Smith" not in names
    assert today._main_line(db, cid, None) == "+12145550198"


def test_saved_support_data_is_cleaned_on_upgrade(tmp_path):
    path = str(tmp_path / "u.db")
    db = DB(path)
    cid = db.company("acme.test", domain="acme.test", name="Acme")
    db.add_contact(cid, "phone", "+19725550100", "company", source_url="https://acme.test/support/")
    db.add_contact(cid, "phone", "+12145550100", "company", source_url="https://acme.test/contact")
    db.conn.execute("INSERT INTO people (company_id, name, name_key, title, first_seen) "
                    "VALUES (?, 'Bob Smith', 'bob smith', 'Customer Care Agent', '2026-01-01')", (cid,))
    db.conn.execute("PRAGMA user_version = 6")
    db.commit()
    from zoomollo import db as dbmod
    dbmod._READY.discard(path)
    db = DB(path)
    assert db.is_suppressed("+19725550100") and not db.is_suppressed("+12145550100")
    assert not db.conn.execute("SELECT 1 FROM people WHERE name = 'Bob Smith'").fetchone()


def test_today_card_lists_the_open_roles(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOOMOLLO_NO_BACKGROUND", "1")
    from zoomollo.web import create_app
    path = str(tmp_path / "t.db")
    db = DB(path)
    save_desk(db, "controller, accountant")
    cid = db.company("acme.test", domain="acme.test", name="Acme")
    db.update_company(cid, city="Dallas, TX", phone="+12145550100", last_crawled=date.today().isoformat())
    recent = (date.today() - timedelta(days=5)).isoformat()
    db.add_job(cid, "website", "1", "Controller", "Dallas, TX", "https://acme.test/jobs/1", recent)
    db.add_job(cid, "website", "2", "Senior Accountant (On-site)", "Dallas, TX", "https://acme.test/jobs/2", recent)
    db.add_job(cid, "website", "3", "Senior Accountant", "Plano, TX", "https://acme.test/jobs/3", recent)
    for i, t in enumerate(("Warehouse Associate", "Forklift Driver", "Receptionist", "Sales Rep", "Buyer",
                           "IT Analyst", "Office Manager")):
        db.add_job(cid, "website", f"w{i}", t, "Dallas, TX", None, recent)
    db.commit()
    roles = today.open_roles(db, cid, ["Controller", "Senior Accountant (On-site)", "Senior Accountant"])
    assert [r["title"] for r in roles[:2]] == ["Controller", "Senior Accountant"] and roles[1]["n"] == 2
    assert roles[0]["age"] == "5d ago" and not roles[-1]["fit"]
    html = create_app(path).test_client().get("/").get_data(as_text=True)
    assert "Open roles (10, 3 on your desk)" in html and "https://acme.test/jobs/1" in html
    assert "Office Manager" in html and "4 more roles" in html
