"""Small web UI: search contacts, browse companies and jobs, queue new crawls."""

from __future__ import annotations

import queue
import threading
from collections import deque

from flask import Flask, Response, redirect, render_template_string, request, url_for

from . import pipeline
from .db import DB

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zoomollo</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e4e4df;--accent:#2f5bea;--chip:#eef1fd}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ececE8;--muted:#9a9a94;--line:#2e2e2b;--accent:#7d9bff;--chip:#232a45}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;gap:24px;align-items:center;padding:14px 20px;border-bottom:1px solid var(--line);background:var(--card);flex-wrap:wrap}
header b{font-size:18px}nav a{margin-right:16px;color:var(--muted);text-decoration:none}nav a.on{color:var(--ink);font-weight:600}
main{max-width:1200px;margin:0 auto;padding:20px 16px}
.stats{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;min-width:110px}
.stat span{display:block;color:var(--muted);font-size:12px}.stat strong{font-size:20px}
form.bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
input,select,textarea,button{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
input[type=search]{flex:1;min-width:200px}button{background:var(--accent);color:#fff;border:none;cursor:pointer}
a.btn{padding:8px 12px;border:1px solid var(--line);border-radius:8px;text-decoration:none;color:var(--ink);background:var(--card)}
.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:12px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.03em}
td.v{font-family:ui-monospace,Menlo,monospace;font-size:13px}.chip{background:var(--chip);border-radius:20px;padding:2px 8px;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
.card h3{margin:0 0 6px}.card p{color:var(--muted);margin:0 0 10px;font-size:13px}.card textarea{width:100%;min-height:90px}
.log{font-family:ui-monospace,monospace;font-size:12px;white-space:pre-wrap;color:var(--muted);max-height:260px;overflow:auto}
a{color:var(--accent)}
</style></head><body>
<header><b>Zoomollo</b><nav>
<a href="{{ url_for('contacts') }}" class="{{ 'on' if tab=='contacts' }}">Contacts</a>
<a href="{{ url_for('companies') }}" class="{{ 'on' if tab=='companies' }}">Companies</a>
<a href="{{ url_for('jobs') }}" class="{{ 'on' if tab=='jobs' }}">Jobs</a>
<a href="{{ url_for('find') }}" class="{{ 'on' if tab=='find' }}">Find</a></nav></header>
<main>
<div class="stats">{% for k,v in stats.items() %}<div class="stat"><span>{{ k }}</span><strong>{{ v }}</strong></div>{% endfor %}
{% if busy %}<div class="stat"><span>queue</span><strong>{{ busy }} running</strong></div>{% endif %}</div>
{{ body|safe }}
</main></body></html>"""

CONTACTS = """
<form class="bar"><input type="search" name="q" value="{{ q }}" placeholder="Search name, email, title, company…">
<select name="type"><option value="">Emails + phones</option>{% for t in ['email','phone'] %}<option {{ 'selected' if t==type_ }}>{{ t }}</option>{% endfor %}</select>
<select name="kind"><option value="">Any kind</option>{% for k in ['person','role','company','guessed'] %}<option {{ 'selected' if k==kind }}>{{ k }}</option>{% endfor %}</select>
<button>Search</button><a class="btn" href="{{ url_for('export', q=q, type=type_, kind=kind) }}">Export CSV</a></form>
<div class="wrap"><table><tr><th>Company</th><th>Name</th><th>Title</th><th>Contact</th><th>Kind</th><th>Source</th><th>Last seen</th></tr>
{% for r in rows %}<tr><td>{{ r.company_name or r.company_key or '' }}</td><td>{{ r.name or '' }}</td><td>{{ r.title or '' }}</td>
<td class="v">{% if r.type=='email' %}<a href="mailto:{{ r.value }}">{{ r.value }}</a>{% else %}<a href="tel:{{ r.value }}">{{ r.value }}</a>{% endif %}</td>
<td><span class="chip">{{ r.kind }}</span></td><td>{% if r.source_url %}<a href="{{ r.source_url }}" target="_blank" rel="noopener">page</a>{% endif %}</td>
<td>{{ r.last_seen[:10] }}</td></tr>{% else %}<tr><td colspan="7">Nothing yet. Use <a href="{{ url_for('find') }}">Find</a> to crawl a company.</td></tr>{% endfor %}
</table></div>"""

COMPANIES = """<div class="wrap"><table><tr><th>Company</th><th>Name</th><th>Emails</th><th>Phones</th><th>Open jobs</th><th>Email format</th><th>Last crawled</th></tr>
{% for c in rows %}<tr><td><a href="{{ url_for('contacts', q=c.key) }}">{{ c.key }}</a></td><td>{{ c.name or '' }}</td><td>{{ c.emails }}</td><td>{{ c.phones }}</td>
<td>{% if c.open_jobs %}<a href="{{ url_for('jobs', q=c.key) }}">{{ c.open_jobs }}</a>{% else %}0{% endif %}</td>
<td class="v">{{ (c.email_pattern ~ '@' ~ c.domain ~ ' (' ~ (c.pattern_confidence*100)|int ~ '%)') if c.email_pattern else '' }}</td>
<td>{{ (c.last_crawled or '')[:10] }}</td></tr>{% else %}<tr><td colspan="7">No companies yet.</td></tr>{% endfor %}</table></div>"""

JOBS = """<form class="bar"><input type="search" name="q" value="{{ q }}" placeholder="Role, company, location…"><button>Search</button></form>
<div class="wrap"><table><tr><th>Company</th><th>Role</th><th>Location</th><th>Board</th><th>Posted</th></tr>
{% for j in rows %}<tr><td><a href="{{ url_for('contacts', q=j.company_key) }}">{{ j.company_name or j.company_key }}</a></td>
<td>{% if j.url %}<a href="{{ j.url }}" target="_blank" rel="noopener">{{ j.title }}</a>{% else %}{{ j.title }}{% endif %}</td>
<td>{{ j.location or '' }}</td><td><span class="chip">{{ j.board }}</span></td><td>{{ (j.posted_at or '')[:10] }}</td></tr>
{% else %}<tr><td colspan="5">No jobs yet.</td></tr>{% endfor %}</table></div>"""

FIND = """<div class="grid">
<form class="card" method="post" action="{{ url_for('enqueue') }}"><h3>Crawl company websites</h3>
<p>One domain per line. Visits contact, team, about and careers pages first, respects robots.txt, waits between requests.</p>
<input type="hidden" name="action" value="crawl"><textarea name="items" placeholder="acme.com&#10;globex.io"></textarea>
<p><label>Max pages per site <input name="max_pages" type="number" value="40" min="1" max="500" style="width:80px"></label></p><button>Start crawl</button></form>
<form class="card" method="post" action="{{ url_for('enqueue') }}"><h3>Import job boards</h3>
<p>board:slug per line. Supported: greenhouse, lever, ashby, workable. A bare slug tries all four.</p>
<input type="hidden" name="action" value="jobs"><textarea name="items" placeholder="greenhouse:stripe&#10;lever:netflix&#10;ashby:openai"></textarea><button>Import</button></form>
<form class="card" method="post" action="{{ url_for('enqueue') }}"><h3>Guess a work email</h3>
<p>Uses the company's learned format when there is one; saved as “guessed” until seen for real.</p>
<input type="hidden" name="action" value="guess"><input name="name" placeholder="Jane Doe" style="width:100%;margin-bottom:8px">
<input name="domain" placeholder="acme.com" style="width:100%;margin-bottom:8px"><button>Guess</button>
{% if guesses %}<div class="log" style="margin-top:10px">{{ guesses|join('\n') }}</div>{% endif %}</form>
<div class="card"><h3>Activity</h3><div class="log">{{ log|join('\n') or 'Idle.' }}</div>
<p style="margin-top:10px"><a href="{{ url_for('find') }}">Refresh</a></p></div></div>"""


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    tasks: "queue.Queue[tuple[str, str, dict]]" = queue.Queue()
    log: deque[str] = deque(maxlen=200)
    state = {"busy": 0}

    def worker():
        db = DB(db_path)  # own connection for the background thread
        while True:
            action, item, opts = tasks.get()
            state["busy"] += 1
            try:
                if action == "crawl":
                    log.appendleft(f"crawling {item}…")
                    r = pipeline.crawl_company(db, item, max_pages=opts.get("max_pages", 40))
                    log.appendleft(f"✓ {item}: {r['pages']} pages, {r['emails']} emails, {r['phones']} phones")
                elif action == "jobs":
                    refs = [item] if ":" in item else None
                    if refs is None:
                        from .jobboards import detect_board
                        refs = [f"{b}:{item}" for b, _ in detect_board(item)] or []
                        if not refs:
                            log.appendleft(f"✗ {item}: no public job board found")
                    for ref in refs:
                        board, _, slug = ref.partition(":")
                        r = pipeline.import_jobs(db, board, slug)
                        log.appendleft(f"✓ {ref}: {r['jobs']} jobs, {r['emails']} emails, {r['phones']} phones")
            except Exception as e:  # noqa: BLE001
                log.appendleft(f"✗ {item}: {e}")
            finally:
                state["busy"] -= 1
                tasks.task_done()

    threading.Thread(target=worker, daemon=True).start()

    def render(tab, tpl, **ctx):
        db = DB(db_path)
        body = render_template_string(tpl, **ctx)
        return render_template_string(PAGE, tab=tab, body=body, stats=db.stats(),
                                      busy=state["busy"] + tasks.qsize())

    @app.get("/")
    def contacts():
        q, t, k = request.args.get("q", ""), request.args.get("type") or None, request.args.get("kind") or None
        return render("contacts", CONTACTS, rows=DB(db_path).search(q, t, k), q=q, type_=t, kind=k)

    @app.get("/companies")
    def companies():
        return render("companies", COMPANIES, rows=DB(db_path).companies())

    @app.get("/jobs")
    def jobs():
        q = request.args.get("q", "")
        return render("jobs", JOBS, rows=DB(db_path).jobs(q), q=q)

    @app.get("/find")
    def find():
        return render("find", FIND, log=list(log), guesses=request.args.getlist("g"))

    @app.post("/find")
    def enqueue():
        action = request.form.get("action")
        if action == "guess":
            name, domain = request.form.get("name", ""), request.form.get("domain", "")
            guesses = pipeline.guess_for_person(DB(db_path), name, domain) if name and domain else []
            return redirect(url_for("find", g=guesses[:5]))
        items = [ln.strip() for ln in request.form.get("items", "").splitlines() if ln.strip()]
        opts = {"max_pages": int(request.form.get("max_pages") or 40)}
        for it in items:
            tasks.put((action, it, opts))
            log.appendleft(f"queued {action} {it}")
        return redirect(url_for("find"))

    @app.get("/export.csv")
    def export():
        data = pipeline.export_csv(DB(db_path), request.args.get("q", ""),
                                   request.args.get("type") or None, request.args.get("kind") or None)
        return Response(data, mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=zoomollo-contacts.csv"})

    return app
