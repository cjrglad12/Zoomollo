from datetime import datetime
from types import SimpleNamespace

from zoomollo import autosend, crm, hiringscore
from zoomollo.db import DB


class FakeSMTP:
    sent: list = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def starttls(self):
        pass

    def login(self, u, p):
        pass

    def send_message(self, m):
        FakeSMTP.sent.append(m)


def setup(tmp_path, monkeypatch, n=4):
    db = DB(str(tmp_path / "a.db"))
    db.set_setting("my_name", "Casey")
    db.set_setting("my_address", "1 Main St, Dallas, TX 75201")
    scores = {}
    for i in range(n):
        cid = db.company(f"co{i}.com", domain=f"co{i}.com", name=f"Co {i}")
        db.add_job(cid, "website", str(i), "Tax Manager", "Dallas", "u", None)
        pid = db.add_person(cid, f"Pat Lee{i}", "Director of Tax", email=f"pat{i}@co{i}.com")
        db.conn.execute("UPDATE people SET email_status = 'found', department = 'finance', seniority = 'director' "
                        "WHERE id = ?", (pid,))
        scores[cid] = SimpleNamespace(total=95 - i * 5, fit_roles=["Tax Manager"], offlimits=None, reasons=[])
    db.commit()
    crm.ensure_starters(db)
    monkeypatch.setattr(hiringscore, "score_all", lambda db, desk: scores)
    FakeSMTP.sent = []
    monkeypatch.setattr(crm.smtplib, "SMTP", FakeSMTP)
    for k, v in {"SMTP_HOST": "smtp.gmail.com", "SMTP_USER": "me@gmail.com", "SMTP_PASSWORD": "pw",
                 "IMAP_HOST": "imap.gmail.com", "EMAIL_DAILY_LIMIT": "40", "EMAIL_CC": "casey@norton.com"}.items():
        monkeypatch.setenv(k, v)
    return db


def test_hourly_batch_best_first_one_per_company(tmp_path, monkeypatch):
    db = setup(tmp_path, monkeypatch)
    autosend.save_settings(db, on=True, per_hour=2)
    r = autosend.run_hour(db, pause=0)
    assert r["sent"] == 2 and r["new"] == 2
    assert [m["To"] for m in FakeSMTP.sent] == ["pat0@co0.com", "pat1@co1.com"]   # highest scores first
    assert "Tax Manager at Co 0" == FakeSMTP.sent[0]["Subject"]
    assert FakeSMTP.sent[0]["Cc"] == "casey@norton.com"                             # EMAIL_CC on every send
    assert db.conn.execute("SELECT COUNT(*) FROM enrollments WHERE auto = 1").fetchone()[0] == 2
    r = autosend.run_hour(db, pause=0)                                             # next hour: the next two
    assert [m["To"] for m in FakeSMTP.sent[2:]] == ["pat2@co2.com", "pat3@co3.com"]
    r = autosend.run_hour(db, pause=0)                                             # nobody left
    assert r["sent"] == 0 and "No new" in r["note"]


def test_off_by_default_and_blockers(tmp_path, monkeypatch):
    db = setup(tmp_path, monkeypatch)
    assert autosend.run_hour(db, pause=0)["sent"] == 0                              # off until turned on
    db.set_setting("my_address", "")
    monkeypatch.delenv("MY_ADDRESS", raising=False)
    assert any("mailing address" in b for b in autosend.blockers(db))
    assert autosend.run_hour(db, force=True, pause=0)["sent"] == 0


def test_pauses_when_too_many_bounce(tmp_path, monkeypatch):
    db = setup(tmp_path, monkeypatch, n=10)
    autosend.save_settings(db, on=True, per_hour=10)
    assert autosend.run_hour(db, pause=0)["sent"] == 10
    for i in range(4):
        db.conn.execute("INSERT INTO bad_emails (value, reason, added_at) VALUES (?, 'bounced', '')", (f"pat{i}@co{i}.com",))
    db.commit()
    r = autosend.run_hour(db, pause=0)
    assert "paused" in r["note"] and autosend.settings(db)["on"] is False


def test_unsent_text_step_does_not_block_followup_emails(tmp_path, monkeypatch):
    db = setup(tmp_path, monkeypatch, n=1)
    db.conn.execute("UPDATE people SET phone = '+12145550134'")
    autosend.save_settings(db, on=True, per_hour=5)
    assert autosend.run_hour(db, pause=0)["sent"] == 1                              # step 1 email; step 2 is a text
    e = db.conn.execute("SELECT * FROM enrollments").fetchone()
    assert e["step_no"] == 2
    db.conn.execute("UPDATE enrollments SET next_due = '2000-01-01', started_at = '2000-01-01'")   # text ignored for days
    db.commit()
    assert autosend.skip_stale_texts(db) == 1
    assert db.conn.execute("SELECT step_no FROM enrollments").fetchone()[0] == 3    # on to the follow-up email


def test_duplicate_company_rows_are_merged(tmp_path):
    db = DB(str(tmp_path / "m.db"))
    a = db.company("examplebank.com", domain="examplebank.com", name="ExampleBank")
    b = db.company("examplebank.com:443", domain="examplebank.com", name="Business & Personal Banking: ExampleBank")
    db.add_job(a, "website", "1", "Controller", "Dallas", "u", None)
    db.add_person(a, "Pat Example", None)
    db.add_person(b, "Pat Example", "President", email="pexample@examplebank.com")
    db.add_person(b, "Dana Wu", "CFO")
    db.commit()
    assert db.merge_duplicate_domains() == 1
    assert [r["name"] for r in db.conn.execute("SELECT name FROM companies")] == ["ExampleBank"]
    people = {r["name"]: r for r in db.conn.execute("SELECT * FROM people WHERE company_id = ?", (a,))}
    assert set(people) == {"Pat Example", "Dana Wu"}
    assert (people["Pat Example"]["title"], people["Pat Example"]["email"]) == ("President", "pexample@examplebank.com")


def test_sending_hours():
    cfg = {**autosend.DEFAULTS, "start": 8, "end": 16}
    assert autosend.in_window(cfg, datetime(2026, 11, 11, 9))       # Wednesday 9am
    assert not autosend.in_window(cfg, datetime(2026, 11, 11, 16))  # 4pm: done for the day
    assert not autosend.in_window(cfg, datetime(2026, 11, 14, 10))  # Saturday
