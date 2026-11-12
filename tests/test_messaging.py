from urllib.parse import unquote

from zoomollo import ai, messaging
from zoomollo.db import DB


def setup(tmp_path):
    db = DB(str(tmp_path / "m.db"))
    cid = db.company("acme.com", domain="acme.com", name="Acme Mechanical")
    db.update_company(cid, city="Austin, TX", phone="+1 512 555 0100")
    pid = db.add_person(cid, "Pat Kim", "Service Manager", phone="+15125550134")
    db.add_person(cid, "No Phone", "Owner")
    db.add_job(cid, "website", "1", "HVAC Service Technician", "Austin", "u", None)
    db.conn.execute("UPDATE people SET hiring_for = 'HVAC Service Technician' WHERE id = ?", (pid,))
    db.set_setting("my_name", "Casey")
    db.set_setting("my_company", "Norton Digital")
    db.commit()
    return db, cid, pid


def test_render_and_links(tmp_path):
    db, cid, pid = setup(tmp_path)
    text, missing = messaging.render(messaging.STARTER_TEMPLATES[0][1], messaging.person_fields(db, pid))
    assert text.startswith("Hi Pat, this is Casey with Norton Digital. I saw Acme Mechanical is hiring an "
                           "HVAC Service Technician in Austin.")
    assert missing == []
    # Company main line: no person, so generic greeting.
    text, missing = messaging.render("Hi {first_name}, about {company}.", messaging.person_fields(db, company_id=cid))
    assert text == "Hi there, about Acme Mechanical." and missing == ["first_name"]
    link = messaging.sms_link("(512) 555-0134", "Hi Pat & team")
    assert link.startswith("sms:+15125550134?&body=") and unquote(link.split("body=")[1]) == "Hi Pat & team"
    assert messaging.qr_svg(link).startswith("<svg")


def test_queue_logging_and_optout(tmp_path):
    db, cid, pid = setup(tmp_path)
    assert [p["name"] for p in messaging.outreach_list(db)] == ["Pat Kim"]
    assert messaging.log_message(db, "+15125550134", "hello", "sms", person_id=pid)
    assert messaging.outreach_list(db) == []                      # already texted
    assert len(messaging.outreach_list(db, include_messaged=True)) == 1
    db.suppress("+15125550134", "opted out")
    assert not messaging.log_message(db, "+15125550134", "again", "sms")


def test_ai_draft_uses_local_model(monkeypatch):
    calls = {}

    class R:
        status_code = 200
        ok = True

        def raise_for_status(self):
            pass

        def json(self):
            return {"response": 'Here is your message:\n"Hi {{first_name}}, saw [Company] is hiring a {Role}. '
                                'Reply STOP to opt out."'}

    def fake_post(url, **kw):
        calls["url"] = url
        calls["model"] = kw["json"]["model"]
        return R()

    monkeypatch.setattr(ai.requests, "post", fake_post)
    out = ai.draft_template("offer a candidate", "warm", "short")
    assert calls["url"] == "http://localhost:11434/api/generate" and calls["model"] == "llama3.2"
    assert out == "Hi {first_name}, saw {company} is hiring a {role}. Reply STOP to opt out."

    def down(url, **kw):
        raise ai.requests.ConnectionError("refused")
    monkeypatch.setattr(ai.requests, "post", down)
    try:
        ai.draft_template("x")
        raise AssertionError("expected AIUnavailable")
    except ai.AIUnavailable as e:
        assert "ollama.com" in str(e)


def test_web_messaging_flow(tmp_path, monkeypatch):
    from zoomollo import web
    from zoomollo.web import create_app
    monkeypatch.setattr(ai, "available", lambda: False)
    monkeypatch.setattr(web, "_local_hour", lambda: 12)  # inside texting hours
    db, cid, pid = setup(tmp_path)
    c = create_app(db.path).test_client()
    page = c.get("/messages").data.decode()
    assert "Open role intro" in page and "Pat Kim" in page and "Not set up yet" in page
    c.post("/messages", data={"action": "save", "name": "Mine", "body": "Yo {first_name} at {company}"})
    tid = DB(db.path).templates()[-1]["id"]
    one = c.get(f"/message?person={pid}&template={tid}").data.decode()
    assert "Yo Pat at Acme Mechanical" in one
    assert "<svg" in one and "Mark as sent" in one                       # QR to send from a phone
    r = c.post("/message/send", data={"phone": "+15125550134", "text": "Yo Pat", "channel": "sms",
                                      "person": pid, "template": tid})
    assert r.status_code == 302 and r.headers["Location"].startswith("sms:+15125550134?&body=Yo")
    r = c.post("/message/send", data={"phone": "+15125550134", "text": "Yo Pat", "channel": "manual",
                                      "person": pid}, headers={"Referer": "/messages"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/messages")
    rows = DB(db.path).conn.execute("SELECT channel FROM messages ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["sms", "phone"]
    assert c.post("/message/qr", data={"phone": "+15125550134", "text": "hi"}).data.startswith(b"<svg")
    assert c.get(f"/message?company={cid}").status_code == 200
    c.post("/message/optout", data={"phone": "+15125550134"})
    r = c.post("/message/send", data={"phone": "+15125550134", "text": "again", "channel": "sms"})
    assert r.status_code == 403


def test_linkedin_buttons(tmp_path):
    from zoomollo.orgmap import clean_linkedin_url, linkedin_search_url
    from zoomollo.web import create_app
    assert linkedin_search_url("Pat Kim", "Acme Mechanical") == \
        "https://www.linkedin.com/search/results/people/?keywords=Pat%20Kim%20Acme%20Mechanical"
    assert clean_linkedin_url("linkedin.com/in/pat-kim-123/?trk=x") == "https://www.linkedin.com/in/pat-kim-123"
    assert clean_linkedin_url("https://www.linkedin.com/company/acme") is None
    db, cid, pid = setup(tmp_path)
    c = create_app(db.path).test_client()
    page = c.get("/people").data.decode()
    assert "Find on LinkedIn" in page and "keywords=Pat%20Kim%20Acme%20Mechanical" in page
    c.post(f"/person/{pid}/linkedin", data={"url": "https://www.linkedin.com/in/patkim/"})
    page = c.get(f"/company/{cid}").data.decode()
    assert 'href="https://www.linkedin.com/in/patkim"' in page
