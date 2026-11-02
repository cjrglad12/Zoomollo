import os
import smtplib

from zoomollo import crm, settings
from zoomollo.db import DB


class FakeSMTP:
    sent = []
    password_ok = "abcdefghijklmnop"

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def starttls(self):
        pass

    def login(self, user, pw):
        if pw != self.password_ok:
            raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")

    def send_message(self, m):
        FakeSMTP.sent.append(m)


class FakeIMAP:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def login(self, u, p):
        pass


def test_gmail_preset(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "casey@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    settings.apply_presets()
    assert os.environ["SMTP_HOST"] == "smtp.gmail.com" and os.environ["IMAP_HOST"] == "imap.gmail.com"
    assert os.environ["SMTP_USER"] == "casey@gmail.com" and os.environ["SMTP_PASSWORD"] == "abcdefghijklmnop"
    assert crm.smtp_configured() and crm.imap_configured()


def test_save_values_keeps_other_lines(tmp_path, monkeypatch):
    f = tmp_path / settings.FILENAME
    f.write_text("# comment\nLOXO_API_KEY=keep\nGMAIL_ADDRESS=old@gmail.com\n")
    monkeypatch.setattr(settings, "settings_path", lambda: f)
    settings.save_values({"GMAIL_ADDRESS": "new@gmail.com", "SMTP_PORT": "587"})
    assert f.read_text() == "# comment\nLOXO_API_KEY=keep\nGMAIL_ADDRESS=new@gmail.com\nSMTP_PORT=587\n"
    assert oct(f.stat().st_mode)[-3:] == "600" and os.environ["GMAIL_ADDRESS"] == "new@gmail.com"


def test_connect_gmail_in_the_app(tmp_path, monkeypatch):
    from zoomollo.web import create_app
    f = tmp_path / settings.FILENAME
    monkeypatch.setattr(settings, "settings_path", lambda: f)
    monkeypatch.setattr(crm.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(crm.imaplib, "IMAP4_SSL", FakeIMAP)
    db = DB(str(tmp_path / "g.db"))
    c = create_app(db.path).test_client()
    assert "Connect Gmail" in c.get("/pipeline").data.decode()

    r = c.post("/email/setup", data={"action": "gmail", "address": "casey@gmail.com", "app_password": "wrong"})
    page = c.get(r.headers["Location"]).data.decode()
    assert "Google rejected the login" in page

    r = c.post("/email/setup", data={"action": "gmail", "address": "casey@gmail.com",
                                     "app_password": "abcd efgh ijkl mnop"})
    page = c.get(r.headers["Location"]).data.decode()
    assert "Gmail connected" in page and "Sending from your mailbox (<b>casey@gmail.com</b>)" in page
    assert "GMAIL_APP_PASSWORD=abcdefghijklmnop" in f.read_text()

    r = c.post("/email/setup", data={"action": "test_send"})
    assert "Test email sent" in c.get(r.headers["Location"]).data.decode()
    assert FakeSMTP.sent[-1]["To"] == "casey@gmail.com"

    c.post("/email/setup", data={"action": "disconnect"})
    assert "GMAIL_APP_PASSWORD=\n" in f.read_text() and not crm.smtp_configured()
