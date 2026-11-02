from email.message import EmailMessage

from zoomollo import crm, emailcheck
from zoomollo.db import DB


def setup(tmp_path, phone=True):
    db = DB(str(tmp_path / "c.db"))
    cid = db.company("acme.com", domain="acme.com", name="Acme Mechanical")
    db.update_company(cid, city="Austin, TX", email_pattern="{first}.{last}")
    pid = db.add_person(cid, "Pat Kim", "Service Manager", phone="+15125550134" if phone else None)
    db.add_job(cid, "website", "1", "HVAC Service Technician", "Austin", "u", None)
    db.conn.execute("UPDATE people SET hiring_for = 'HVAC Service Technician' WHERE id = ?", (pid,))
    db.set_setting("my_name", "Casey")
    db.set_setting("my_company", "Norton Digital")
    db.set_setting("my_address", "1 Main St, Austin, TX 78701")
    db.commit()
    emailcheck.guess_all(db, cid)          # pat.kim@acme.com (guessed)
    crm.ensure_starters(db)
    return db, cid, pid, crm.sequences(db)[0]["id"]


def raw(frm, subject, body, **headers):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, "casey@nd.com", subject
    m["Message-ID"] = f"<{abs(hash(frm + subject + body))}@x>"
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content(body)
    return m.as_bytes()


def test_pipeline_stages_and_notes(tmp_path):
    db, cid, pid, _ = setup(tmp_path)
    crm.add_to_pipeline(db, cid, pid)
    crm.update_card(db, cid, next_step="Call Tuesday", due_date="2000-01-01")
    crm.add_note(db, cid, "Spoke to reception")
    crm.advance_to(db, cid, "contacted")
    crm.advance_to(db, cid, "prospect")                               # never moves backwards
    assert crm.card(db, cid)["stage"] == "contacted"
    assert any(i["kind"] == "task" and i["next_step"] == "Call Tuesday" for i in crm.due_items(db))
    crm.task_done(db, cid)
    assert not any(i["kind"] == "task" for i in crm.due_items(db))
    kinds = [a["kind"] for a in crm.timeline(db, cid)]
    assert "note" in kinds and "stage" in kinds
    assert [c["company_name"] for c in crm.board(db)["contacted"]] == ["Acme Mechanical"]


def test_sequence_flow_email_then_text(tmp_path):
    db, cid, pid, seq = setup(tmp_path)
    eid = crm.enroll(db, pid, seq)
    [step] = [i for i in crm.due_items(db) if i["kind"] == "step"]
    assert step["channel"] == "email" and step["to"] == "pat.kim@acme.com"
    assert step["subject"] == "HVAC Service Technician at Acme Mechanical"
    assert "1 Main St, Austin" in step["body"] and 'Reply "unsubscribe"' in step["body"]
    link = crm.mailto_link(step["to"], step["subject"], step["body"])
    assert link.startswith("mailto:pat.kim@acme.com?subject=HVAC%20Service")
    crm.complete_step(db, eid, "mailto", step["subject"], step["body"], step["to"])
    assert crm.card(db, cid)["stage"] == "contacted"
    e = db.conn.execute("SELECT * FROM enrollments WHERE id = ?", (eid,)).fetchone()
    assert e["step_no"] == 2 and e["next_due"] > crm.today()          # day-3 text is scheduled
    db.conn.execute("UPDATE enrollments SET next_due = ? WHERE id = ?", (crm.today(), eid))
    [text] = [i for i in crm.due_items(db) if i["kind"] == "step"]
    assert text["channel"] == "text" and text["to"] == "+15125550134" and text["body"].startswith("Hi Pat")


def test_text_steps_skipped_without_phone(tmp_path):
    db, cid, pid, seq = setup(tmp_path, phone=False)
    eid = crm.enroll(db, pid, seq)
    step = [i for i in crm.due_items(db) if i["kind"] == "step"][0]
    crm.complete_step(db, eid, "mailto", step["subject"], step["body"], step["to"])
    db.conn.execute("UPDATE enrollments SET next_due = ? WHERE id = ?", (crm.today(), eid))
    db.commit()
    assert [i for i in crm.due_items(db) if i["kind"] == "step"] == []   # text step skipped, email moved on
    assert db.conn.execute("SELECT step_no FROM enrollments WHERE id = ?", (eid,)).fetchone()[0] == 3


def test_smtp_sending_and_daily_limit(tmp_path, monkeypatch):
    db, cid, pid, seq = setup(tmp_path)
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def starttls(self):
            pass

        def login(self, u, p):
            assert (u, p) == ("casey@nd.com", "app-pass")

        def send_message(self, m):
            sent.append(m)

    monkeypatch.setattr(crm.smtplib, "SMTP", FakeSMTP)
    for k, v in {"SMTP_HOST": "smtp.nd.com", "SMTP_USER": "casey@nd.com", "SMTP_PASSWORD": "app-pass",
                 "EMAIL_DAILY_LIMIT": "1"}.items():
        monkeypatch.setenv(k, v)
    crm.enroll(db, pid, seq)
    r = crm.send_due_emails(db)
    assert r == {"sent": 1, "failed": 0} and sent[0]["To"] == "pat.kim@acme.com"
    assert sent[0]["From"] == "Casey <casey@nd.com>" and "unsubscribe" in sent[0]["List-Unsubscribe"]
    assert tuple(db.conn.execute("SELECT method, message_id IS NOT NULL FROM emails").fetchone()) == ("smtp", 1)
    try:
        crm.send_smtp(db, "x@acme.com", "s", "b")
        raise AssertionError("limit not enforced")
    except RuntimeError as e:
        assert "Daily email limit" in str(e)


def test_inbox_reply_unsubscribe_autoreply_bounce(tmp_path):
    db, cid, pid, seq = setup(tmp_path)
    eid = crm.enroll(db, pid, seq)
    step = [i for i in crm.due_items(db) if i["kind"] == "step"][0]
    crm.complete_step(db, eid, "smtp", step["subject"], step["body"], step["to"])

    # Out-of-office: logged, follow-ups keep going.
    assert crm.process_message(db, raw("Pat Kim <pat.kim@acme.com>", "Automatic reply: away", "back Monday",
                                       Auto_Submitted="auto-replied")) is None
    assert db.conn.execute("SELECT status FROM enrollments WHERE id = ?", (eid,)).fetchone()[0] == "active"

    # Bounce: next guess, and the bounced step is resent to it.
    bounce = raw("Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "Delivery Status Notification (Failure)",
                 "Address not found: pat.kim@acme.com couldn't be found.")
    assert crm.process_message(db, bounce) == "bounce"
    assert crm.process_message(db, bounce) is None                    # same message isn't processed twice
    p = db.conn.execute("SELECT email FROM people WHERE id = ?", (pid,)).fetchone()
    e = db.conn.execute("SELECT step_no, next_due FROM enrollments WHERE id = ?", (eid,)).fetchone()
    assert p["email"] == "pat@acme.com" and tuple(e) == (1, crm.today())

    # A real reply: stage moves to Replied and follow-ups stop.
    db.conn.execute("UPDATE people SET email = 'pat.kim@acme.com' WHERE id = ?", (pid,))
    db.commit()
    assert crm.process_message(db, raw("pat.kim@acme.com", "Re: HVAC", "Sure, call me Thursday.\n\nOn Mon X wrote:\n> hi")) == "reply"
    assert crm.card(db, cid)["stage"] == "replied"
    assert db.conn.execute("SELECT status FROM enrollments WHERE id = ?", (eid,)).fetchone()[0] == "replied"
    assert any("call me Thursday" in a["body"] and "wrote" not in a["body"] for a in crm.timeline(db, cid))


def test_unsubscribe_reply_blocks_address(tmp_path):
    db, cid, pid, seq = setup(tmp_path)
    eid = crm.enroll(db, pid, seq)
    step = [i for i in crm.due_items(db) if i["kind"] == "step"][0]
    crm.complete_step(db, eid, "mailto", step["subject"], step["body"], step["to"])
    assert crm.process_message(db, raw("pat.kim@acme.com", "Re: x", "Please unsubscribe me")) == "unsubscribe"
    assert db.is_suppressed("pat.kim@acme.com")
    assert db.conn.execute("SELECT status FROM enrollments WHERE id = ?", (eid,)).fetchone()[0] == "unsubscribed"


def test_web_pipeline_pages(tmp_path):
    from zoomollo.web import create_app
    db, cid, pid, seq = setup(tmp_path)
    c = create_app(db.path).test_client()
    assert c.post("/enroll", data={"person": pid, "sequence": seq}).status_code == 302
    page = c.get("/pipeline").data.decode()
    assert "Due today" in page and "pat.kim@acme.com" in page and "Open in email app" in page
    eid = db.conn.execute("SELECT id FROM enrollments").fetchone()[0]
    r = c.post(f"/due/{eid}", data={"action": "mailto", "subject": "Hi", "body": "Edited body"})
    assert r.headers["Location"].startswith("mailto:pat.kim@acme.com?subject=Hi&body=Edited%20body")
    comp = c.get(f"/company/{cid}").data.decode()
    assert "Timeline" in comp and "Edited body" in comp and "Contacted" in comp
    c.post(f"/offlimits/{cid}", data={"action": "set", "reason": "partner's client"})
    assert "⛔ Off-limits" in c.get("/people").data.decode()
    r = c.post("/message/send", data={"phone": "+15125550134", "text": "hi", "channel": "sms", "person": pid})
    assert r.status_code == 403
    r = c.post("/message/send", data={"phone": "+15125550134", "text": "hi", "channel": "sms", "person": pid,
                                      "override": "1"})
    assert r.status_code == 302
