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
        host = self.headers.get("X-Test-Host", "acme.test").removeprefix("www.")
        path = self.path.split("?")[0].rstrip("/") or "/"
        body = self.server.pages.get(host + path)
        if body is None and host == "acme.test":
            body = self.server.pages.get(path)
        if body is None:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        ctype = ("text/plain" if self.path.endswith(".txt") else "application/xml" if self.path.endswith(".xml")
                 else "application/json" if self.path.split("?")[0].endswith(".json") else "text/html")
        self.send_header("Content-Type", ctype)
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
    # Route https://<anything>.test/... to the local server, telling it which site was asked for.
    orig = crawler_mod.requests.Session.get
    local = f"http://127.0.0.1:{port}"

    def fake_get(self, url, **kw):
        from urllib.parse import urlparse
        u = urlparse(url)
        if not u.netloc.endswith(".test"):
            raise crawler_mod.requests.ConnectionError(f"no network in tests: {url}")
        kw.setdefault("headers", {})
        kw["headers"] = {**kw["headers"], "X-Test-Host": u.netloc}
        r = orig(self, local + url[len(f"{u.scheme}://{u.netloc}"):], **kw)
        r.url = r.url.replace(local, f"https://{u.netloc}")
        return r

    monkeypatch.setattr(crawler_mod.requests.Session, "get", fake_get)
    monkeypatch.setattr(crawler_mod.requests, "get", lambda url, **kw: fake_get(crawler_mod.requests.Session(), url, **kw))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    yield srv.pages
    srv.shutdown()


