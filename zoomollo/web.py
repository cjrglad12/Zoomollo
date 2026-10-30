"""Web UI: prospect by industry + city, browse org maps, people, contacts and jobs."""

from __future__ import annotations

import queue
import threading
from collections import deque

from flask import Flask, Response, abort, redirect, render_template_string, request, url_for

from . import pipeline
from .db import DB
from .orgmap import hiring_managers
from .search import get_provider

DEPT_LABELS = {
    "executive": "Leadership", "sales": "Sales", "marketing": "Marketing", "operations": "Operations",
    "engineering": "Engineering", "product": "Product & Design", "finance": "Finance",
    "hr": "HR & Recruiting", "customer": "Customer", "it": "IT", "legal": "Legal", "other": "Other",
}
LEVEL_LABELS = {"c_level": "Exec", "vp": "VP", "director": "Director", "manager": "Manager", "staff": "Staff"}

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zoomollo</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e4e4df;--accent:#2f5bea;--chip:#eef1fd;--hm:#fff4d6;--hmink:#7a5300}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ececE8;--muted:#9a9a94;--line:#2e2e2b;--accent:#7d9bff;--chip:#232a45;--hm:#3a2f12;--hmink:#f3cd6d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;gap:20px;align-items:center;padding:14px 16px;border-bottom:1px solid var(--line);background:var(--card);flex-wrap:wrap}
header b{font-size:18px}nav{display:flex;gap:14px;flex-wrap:wrap}nav a{color:var(--muted);text-decoration:none}nav a.on{color:var(--ink);font-weight:600}
main{max-width:1240px;margin:0 auto;padding:20px 16px}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 14px;min-width:96px}
.stat span{display:block;color:var(--muted);font-size:12px}.stat strong{font-size:19px}
form.bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
input,select,textarea,button{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
input[type=search]{flex:1;min-width:180px}button{background:var(--accent);color:#fff;border:none;cursor:pointer}
a.btn{padding:8px 12px;border:1px solid var(--line);border-radius:8px;text-decoration:none;color:var(--ink);background:var(--card)}
.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);white-space:nowrap;vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.03em}
td.v,.v{font-family:ui-monospace,Menlo,monospace;font-size:13px}.chip{background:var(--chip);border-radius:20px;padding:2px 8px;font-size:12px;white-space:nowrap}
.hm{background:var(--hm);color:var(--hmink)}.muted{color:var(--muted)}td.wraptext{white-space:normal;min-width:200px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
.card h3{margin:0 0 6px}.card p{color:var(--muted);margin:0 0 10px;font-size:13px}.card textarea{width:100%;min-height:80px}
.card.big{grid-column:1/-1}.row{display:flex;gap:8px;flex-wrap:wrap}.row input{flex:1;min-width:160px}
.log{font-family:ui-monospace,monospace;font-size:12px;white-space:pre-wrap;color:var(--muted);max-height:300px;overflow:auto}
.org{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px}
.dept h4{margin:0 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.person{border-top:1px solid var(--line);padding:8px 0}.person:first-of-type{border-top:none}
.person .n{font-weight:600}.person .t{color:var(--muted);font-size:13px}.person .c{font-size:13px;margin-top:2px;word-break:break-all}
h2{margin:0 0 4px}.sub{color:var(--muted);margin-bottom:16px}.section{margin:22px 0 10px;font-size:15px}
a{color:var(--accent)}.warn{background:var(--hm);color:var(--hmink);padding:10px 12px;border-radius:8px;font-size:13px;margin-bottom:12px}
</style></head><body>
<header><b>Zoomollo</b><nav>
{% for t,label in [('find','Prospect'),('people','People'),('companies','Companies'),('contacts','Contacts'),('jobs','Jobs')] %}
<a href="{{ url_for(t) }}" class="{{ 'on' if tab==t }}">{{ label }}</a>{% endfor %}</nav></header>
<main>
<div class="stats">{% for k,v in stats.items() %}<div class="stat"><span>{{ k }}</span><strong>{{ v }}</strong></div>{% endfor %}
{% if busy %}<div class="stat"><span>queue</span><strong>{{ busy }} running</strong></div>{% endif %}</div>
{{ body|safe }}
</main></body></html>"""

FIND = """<div class="grid">
<form class="card big" method="post" action="{{ url_for('enqueue') }}"><h3>Prospect</h3>
<p>Describe the companies and the city. Zoomollo finds them, crawls their sites and job boards, maps their people and flags likely hiring managers.</p>
{% if not provider %}<div class="warn">No search API set, so only OpenStreetMap and company websites are used. Set BRAVE_API_KEY (free tier), SERPAPI_KEY or SEARXNG_URL to find more companies and people.</div>{% endif %}
<input type="hidden" name="action" value="prospect">
<div class="row"><input name="what" placeholder="What they do: commercial HVAC, law firms, SaaS…" required>
<input name="where" placeholder="City: Austin, TX" required>
<input name="limit" type="number" value="25" min="1" max="500" style="max-width:110px" title="Max companies">
<button>Find companies</button></div></form>
<div class="card"><h3>Activity</h3><div class="log">{{ log|join('\\n') or 'Idle.' }}</div>
<p style="margin-top:10px"><a href="{{ url_for('find') }}">Refresh</a></p></div>
<form class="card" method="post" action="{{ url_for('enqueue') }}"><h3>Crawl specific sites</h3>
<p>One domain per line.</p><input type="hidden" name="action" value="crawl">
<textarea name="items" placeholder="acme.com"></textarea><button>Crawl</button></form>
<form class="card" method="post" action="{{ url_for('enqueue') }}"><h3>Import job boards</h3>
<p>board:slug per line (greenhouse, lever, ashby, workable), or a bare slug to try all.</p>
<input type="hidden" name="action" value="jobs"><textarea name="items" placeholder="greenhouse:stripe"></textarea><button>Import</button></form>
<form class="card" method="post" action="{{ url_for('enqueue') }}"><h3>Guess a work email</h3>
<input type="hidden" name="action" value="guess"><input name="name" placeholder="Jane Doe" style="width:100%;margin-bottom:8px">
<input name="domain" placeholder="acme.com" style="width:100%;margin-bottom:8px"><button>Guess</button>
{% if guesses %}<div class="log" style="margin-top:10px">{{ guesses|join('\\n') }}</div>{% endif %}</form>
</div>"""

PEOPLE = """<form class="bar"><input type="search" name="q" value="{{ q }}" placeholder="Name, title, company, city, industry…">
<select name="department"><option value="">All departments</option>{% for k,v in depts.items() %}<option value="{{ k }}" {{ 'selected' if k==dept }}>{{ v }}</option>{% endfor %}</select>
<label class="chip" style="padding:8px 10px"><input type="checkbox" name="hiring" value="1" {{ 'checked' if hiring }} style="padding:0"> Hiring managers</label>
<button>Search</button><a class="btn" href="{{ url_for('export_people', q=q, department=dept, hiring=hiring) }}">Export CSV</a></form>
<div class="wrap"><table><tr><th>Name</th><th>Title</th><th>Company</th><th>Email</th><th>Phone</th><th>Hiring for</th><th></th></tr>
{% for p in rows %}<tr><td><b>{{ p.name }}</b></td><td>{{ p.title or '' }}</td>
<td><a href="{{ url_for('company', cid=p.company_id) }}">{{ p.company_name or p.company_key }}</a></td>
<td class="v">{% if p.email %}<a href="mailto:{{ p.email }}">{{ p.email }}</a> <span class="chip">{{ p.email_status }}</span>{% endif %}</td>
<td class="v">{{ p.phone or '' }}</td><td class="wraptext">{% if p.hiring_for %}<span class="chip hm">{{ p.hiring_for }}</span>{% endif %}</td>
<td>{% if p.linkedin_url %}<a href="{{ p.linkedin_url }}" target="_blank" rel="noopener">profile</a>{% endif %}</td></tr>
{% else %}<tr><td colspan="7">No people yet. Start on the <a href="{{ url_for('find') }}">Prospect</a> tab.</td></tr>{% endfor %}</table></div>"""

COMPANY = """<h2>{{ co.name or co.key }}</h2>
<div class="sub">{% if co.domain %}<a href="https://{{ co.domain }}" target="_blank" rel="noopener">{{ co.domain }}</a> · {% endif %}
{% if co.industry %}{{ co.industry }} · {% endif %}{% if co.city %}{{ co.city }} · {% endif %}
{% if co.phone %}<span class="v">{{ co.phone }}</span> · {% endif %}{% if co.address %}{{ co.address }} · {% endif %}
{% if co.email_pattern %}email format <span class="v">{{ co.email_pattern }}@{{ co.domain }}</span> ({{ (co.pattern_confidence*100)|int }}%){% endif %}</div>

<div class="section"><b>Org map</b> <span class="muted">· {{ people|length }} people</span></div>
{% if people %}<div class="org">{% for d, ps in org %}<div class="card dept"><h4>{{ depts[d] }}</h4>
{% for p in ps %}<div class="person"><div class="n">{{ p.name }} {% if p.hiring_for %}<span class="chip hm">hiring</span>{% endif %}</div>
<div class="t">{{ p.title or '' }}{% if p.seniority %} · {{ levels[p.seniority] }}{% endif %}</div>
<div class="c v">{% if p.email %}<a href="mailto:{{ p.email }}">{{ p.email }}</a> <span class="chip">{{ p.email_status }}</span>{% endif %}
{% if p.phone %}<br>{{ p.phone }}{% endif %}{% if p.linkedin_url %}<br><a href="{{ p.linkedin_url }}" target="_blank" rel="noopener">profile</a>{% endif %}</div>
</div>{% endfor %}</div>{% endfor %}</div>
{% else %}<p class="muted">No people found yet.</p>{% endif %}

<div class="section"><b>Open roles & likely hiring managers</b> <span class="muted">· {{ jobs|length }}</span></div>
<div class="wrap"><table><tr><th>Role</th><th>Location</th><th>Likely hiring manager</th></tr>
{% for j in jobs %}<tr><td>{% if j.url %}<a href="{{ j.url }}" target="_blank" rel="noopener">{{ j.title }}</a>{% else %}{{ j.title }}{% endif %}</td>
<td>{{ j.location or '' }}</td><td class="wraptext">{% for p in hm.get(j.id, []) %}{{ p.name }} <span class="muted">({{ p.title or '' }})</span>{% if p.email %} <span class="v">{{ p.email }}</span>{% endif %}<br>{% else %}<span class="muted">unknown</span>{% endfor %}</td></tr>
{% else %}<tr><td colspan="3">No open roles found.</td></tr>{% endfor %}</table></div>

<div class="section"><b>Company contacts</b></div>
<div class="wrap"><table><tr><th>Contact</th><th>Kind</th><th>Source</th></tr>
{% for r in contacts %}<tr><td class="v">{{ r.value }}</td><td><span class="chip">{{ r.kind }}</span></td>
<td>{% if r.source_url and r.source_url.startswith('http') %}<a href="{{ r.source_url }}" target="_blank" rel="noopener">page</a>{% else %}{{ r.source_url or '' }}{% endif %}</td></tr>
{% else %}<tr><td colspan="3">None.</td></tr>{% endfor %}</table></div>"""

COMPANIES = """<form class="bar"><input type="search" name="q" value="{{ q }}" placeholder="Company, city, industry…"><button>Search</button></form>
<div class="wrap"><table><tr><th>Company</th><th>City</th><th>Industry</th><th>People</th><th>Emails</th><th>Phones</th><th>Open jobs</th><th>Email format</th></tr>
{% for c in rows %}<tr><td><a href="{{ url_for('company', cid=c.id) }}">{{ c.name or c.key }}</a>{% if c.domain %} <span class="muted">{{ c.domain }}</span>{% endif %}</td>
<td>{{ c.city or '' }}</td><td>{{ c.industry or '' }}</td><td>{{ c.people }}</td><td>{{ c.emails }}</td><td>{{ c.phones }}</td><td>{{ c.open_jobs }}</td>
<td class="v">{{ c.email_pattern or '' }}</td></tr>{% else %}<tr><td colspan="8">No companies yet.</td></tr>{% endfor %}</table></div>"""

CONTACTS = """<form class="bar"><input type="search" name="q" value="{{ q }}" placeholder="Search name, email, title, company…">
<select name="type"><option value="">Emails + phones</option>{% for t in ['email','phone'] %}<option {{ 'selected' if t==type_ }}>{{ t }}</option>{% endfor %}</select>
<select name="kind"><option value="">Any kind</option>{% for k in ['person','role','company','guessed'] %}<option {{ 'selected' if k==kind }}>{{ k }}</option>{% endfor %}</select>
<button>Search</button><a class="btn" href="{{ url_for('export', q=q, type=type_, kind=kind) }}">Export CSV</a></form>
<div class="wrap"><table><tr><th>Company</th><th>Name</th><th>Title</th><th>Contact</th><th>Kind</th><th>Source</th></tr>
{% for r in rows %}<tr><td><a href="{{ url_for('company', cid=r.company_id) }}">{{ r.company_name or r.company_key or '' }}</a></td><td>{{ r.name or '' }}</td><td>{{ r.title or '' }}</td>
<td class="v">{{ r.value }}</td><td><span class="chip">{{ r.kind }}</span></td>
<td>{% if r.source_url and r.source_url.startswith('http') %}<a href="{{ r.source_url }}" target="_blank" rel="noopener">page</a>{% endif %}</td></tr>
{% else %}<tr><td colspan="6">Nothing yet.</td></tr>{% endfor %}</table></div>"""

JOBS = """<form class="bar"><input type="search" name="q" value="{{ q }}" placeholder="Role, company, location…"><button>Search</button></form>
<div class="wrap"><table><tr><th>Company</th><th>Role</th><th>Department</th><th>Location</th><th>Board</th></tr>
{% for j in rows %}<tr><td><a href="{{ url_for('company', cid=j.company_id) }}">{{ j.company_name or j.company_key }}</a></td>
<td>{% if j.url %}<a href="{{ j.url }}" target="_blank" rel="noopener">{{ j.title }}</a>{% else %}{{ j.title }}{% endif %}</td>
<td>{{ depts.get(j.department or 'other', '') }}</td><td>{{ j.location or '' }}</td><td><span class="chip">{{ j.board }}</span></td></tr>
{% else %}<tr><td colspan="5">No jobs yet.</td></tr>{% endfor %}</table></div>"""


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    tasks: "queue.Queue[tuple[str, str, dict]]" = queue.Queue()
    log: deque[str] = deque(maxlen=300)
    state = {"busy": 0}

    def worker():
        db = DB(db_path)  # own connection for the background thread
        while True:
            action, item, opts = tasks.get()
            state["busy"] += 1
            try:
                if action == "prospect":
                    r = pipeline.prospect(db, opts["what"], opts["where"], opts.get("limit", 25),
                                          provider=get_provider(), progress=log.appendleft)
                    log.appendleft(f"✓ done: {r['companies']} companies, {r['people']} people, "
                                   f"{r['hiring_managers']} hiring managers, {r['jobs']} jobs")
                elif action == "crawl":
                    log.appendleft(f"crawling {item}…")
                    r = pipeline.crawl_company(db, item, max_pages=40)
                    log.appendleft(f"✓ {item}: {r['pages']} pages, {r['emails']} emails, {r['people']} people")
                elif action == "jobs":
                    refs = [item] if ":" in item else None
                    if refs is None:
                        from .jobboards import detect_board
                        refs = [f"{b}:{item}" for b, _ in detect_board(item)]
                        if not refs:
                            log.appendleft(f"✗ {item}: no public job board found")
                    for ref in refs:
                        board, _, slug = ref.partition(":")
                        r = pipeline.import_jobs(db, board, slug)
                        log.appendleft(f"✓ {ref}: {r['jobs']} jobs, {r['emails']} emails")
            except Exception as e:  # noqa: BLE001
                log.appendleft(f"✗ {item}: {e}")
            finally:
                state["busy"] -= 1
                tasks.task_done()

    threading.Thread(target=worker, daemon=True).start()

    def render(tab, tpl, **ctx):
        db = DB(db_path)
        body = render_template_string(tpl, depts=DEPT_LABELS, levels=LEVEL_LABELS, **ctx)
        return render_template_string(PAGE, tab=tab, body=body, stats=db.stats(),
                                      busy=state["busy"] + tasks.qsize())

    @app.get("/")
    def find():
        return render("find", FIND, log=list(log), guesses=request.args.getlist("g"),
                      provider=get_provider() is not None)

    @app.post("/")
    def enqueue():
        action = request.form.get("action")
        if action == "guess":
            name, domain = request.form.get("name", ""), request.form.get("domain", "")
            guesses = pipeline.guess_for_person(DB(db_path), name, domain) if name and domain else []
            return redirect(url_for("find", g=guesses[:5]))
        if action == "prospect":
            what, where = request.form.get("what", "").strip(), request.form.get("where", "").strip()
            if what and where:
                tasks.put(("prospect", f"{what} in {where}",
                           {"what": what, "where": where, "limit": int(request.form.get("limit") or 25)}))
                log.appendleft(f"queued: {what} in {where}")
            return redirect(url_for("find"))
        for it in [ln.strip() for ln in request.form.get("items", "").splitlines() if ln.strip()]:
            tasks.put((action, it, {}))
            log.appendleft(f"queued {action} {it}")
        return redirect(url_for("find"))

    @app.get("/people")
    def people():
        q, d, h = request.args.get("q", ""), request.args.get("department") or None, bool(request.args.get("hiring"))
        return render("people", PEOPLE, rows=DB(db_path).people(q=q, department=d, hiring_only=h, limit=1000),
                      q=q, dept=d, hiring=h)

    @app.get("/company/<int:cid>")
    def company(cid: int):
        db = DB(db_path)
        co = db.company_row(cid)
        if not co:
            abort(404)
        ppl = [dict(p) for p in db.people(cid)]
        jobs = [dict(j) for j in db.jobs(company_id=cid, limit=1000)]
        groups: dict[str, list] = {}
        for p in ppl:
            groups.setdefault(p["department"] or "other", []).append(p)
        org = sorted(groups.items(), key=lambda kv: list(DEPT_LABELS).index(kv[0]) if kv[0] in DEPT_LABELS else 99)
        contacts = [r for r in db.search(limit=10_000) if r["company_id"] == cid and r["kind"] in ("role", "company")]
        return render("companies", COMPANY, co=co, people=ppl, org=org, jobs=jobs,
                      hm=hiring_managers(ppl, jobs), contacts=contacts)

    @app.get("/companies")
    def companies():
        q = request.args.get("q", "").lower()
        rows = [c for c in DB(db_path).companies()
                if not q or any(q in (c[k] or "").lower() for k in ("key", "name", "city", "industry"))]
        return render("companies", COMPANIES, rows=rows, q=q)

    @app.get("/contacts")
    def contacts():
        q, t, k = request.args.get("q", ""), request.args.get("type") or None, request.args.get("kind") or None
        return render("contacts", CONTACTS, rows=DB(db_path).search(q, t, k), q=q, type_=t, kind=k)

    @app.get("/jobs")
    def jobs():
        q = request.args.get("q", "")
        return render("jobs", JOBS, rows=DB(db_path).jobs(q), q=q)

    @app.get("/export.csv")
    def export():
        data = pipeline.export_csv(DB(db_path), request.args.get("q", ""),
                                   request.args.get("type") or None, request.args.get("kind") or None)
        return Response(data, mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=zoomollo-contacts.csv"})

    @app.get("/people.csv")
    def export_people():
        data = pipeline.export_people_csv(DB(db_path), request.args.get("q", ""),
                                          request.args.get("department") or None,
                                          request.args.get("hiring") in ("1", "True", "true"))
        return Response(data, mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=zoomollo-people.csv"})

    return app
