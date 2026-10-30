import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler

import pytest

from zoomollo import crawler as crawler_mod
from zoomollo import pipeline
from zoomollo.db import DB

PAGES = {
    "/": '<title>Acme Inc - Home</title><a href="/our-team">Team</a><a href="/blog/post">Blog</a>',
    "/our-team": '<a href="mailto:jane.doe@acme.test">Jane Doe</a><a href="mailto:bob.smith@acme.test">Bob Smith</a>'
                 '<p>Main line (212) 555-0198. hr@acme.test</p>',
    "/blog/post": "<p>nothing here</p>",
    "/robots.txt": "User-agent: *\nDisallow: /private",
}


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path.rstrip("/") or "/")
        if body is None:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain" if self.path.endswith(".txt") else "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def site(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    # Route https://acme.test/... to the local server.
    orig = crawler_mod.requests.Session.get

    def fake_get(self, url, **kw):
        url = url.replace("https://acme.test", f"http://127.0.0.1:{port}")
        r = orig(self, url, **kw)
        r.url = r.url.replace(f"http://127.0.0.1:{port}", "https://acme.test")
        return r

    monkeypatch.setattr(crawler_mod.requests.Session, "get", fake_get)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    yield
    srv.shutdown()


def test_crawl_end_to_end(site, tmp_path):
    db = DB(str(tmp_path / "t.db"))
    res = pipeline.crawl_company(db, "acme.test", max_pages=10, delay=0)
    assert res["emails"] == 3 and res["phones"] == 1
    rows = {r["value"]: r for r in db.search()}
    assert rows["jane.doe@acme.test"]["name"] == "Jane Doe"
    assert rows["hr@acme.test"]["kind"] == "role"
    co = db.companies()[0]
    assert co["email_pattern"] == "{first}.{last}" and co["name"] == "Acme Inc"

    # Guess uses learned pattern; suppression removes and blocks.
    guesses = pipeline.guess_for_person(db, "Sam Lee", "acme.test", save=False)
    assert guesses[0] == "sam.lee@acme.test"
    assert db.suppress("jane.doe@acme.test") == 1
    assert not db.add_contact(1, "email", "jane.doe@acme.test", "person")
    db.suppress("@acme.test")
    assert not any(r["type"] == "email" for r in db.search())
    assert "hr@acme.test" not in pipeline.export_csv(db)


def test_web_ui(tmp_path):
    from zoomollo.web import create_app
    db = DB(str(tmp_path / "w.db"))
    cid = db.company("acme.com", domain="acme.com")
    db.add_contact(cid, "email", "jane@acme.com", "person", name="Jane")
    db.commit()
    c = create_app(db.path).test_client()
    assert b"jane@acme.com" in c.get("/").data
    assert c.get("/companies").status_code == 200
    assert c.get("/jobs").status_code == 200
    assert b"jane@acme.com" in c.get("/export.csv").data
