import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from zoomollo import crawler as crawler_mod

PAGES = {
    "/": '<title>Acme Inc - Home</title><a href="/our-team">Team</a><a href="/blog/post">Blog</a>',
    "/our-team": '<a href="mailto:jane.doe@acme.test">Jane Doe</a><a href="mailto:bob.smith@acme.test">Bob Smith</a>'
                 '<p>Main line (212) 555-0198. hr@acme.test</p>',
    "/blog/post": "<p>nothing here</p>",
    "/robots.txt": "User-agent: *\nDisallow: /private",
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = self.server.pages.get(self.path.rstrip("/") or "/")
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
    srv.pages = dict(PAGES)
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
    yield srv.pages
    srv.shutdown()


