"""Web UI: prospect by industry + city, browse org maps, people, contacts and jobs."""

from __future__ import annotations

import queue
import threading
from collections import deque

from flask import Flask, Response, abort, redirect, render_template_string, request, url_for

from . import pipeline
from .db import DB
from .search import get_provider

DEPT_LABELS = {
    "executive": "Leadership", "sales": "Sales", "marketing": "Marketing", "operations": "Operations",
    "engineering": "Engineering", "product": "Product & Design", "finance": "Finance",
    "hr": "HR & Recruiting", "customer": "Customer", "it": "IT", "legal": "Legal", "other": "Other",
}
LEVEL_LABELS = {"c_level": "Exec", "vp": "VP", "director": "Director", "manager": "Manager", "staff": "Staff"}

EMAIL_MACRO = """{% macro email_cell(p, verify) %}
{% if p.email %}<a href="mailto:{{ p.email }}">{{ p.email }}</a>
<span class="chip st-{{ p.email_status }}">{{ p.email_status }}{% if p.email_status == 'guessed' %} · try {{ (p.email_attempts or 0) + 1 }}/10{% endif %}</span>
{% if p.email_status != 'confirmed' %}<form class="fb" method="post" action="{{ url_for('email_feedback', pid=p.id, action='good') }}"><button title="Correct">✓</button></form>
<form class="fb" method="post" action="{{ url_for('email_feedback', pid=p.id, action='bad') }}"><button class="no" title="Wrong / bounced: try the next guess">✗</button></form>
{% if verify %}<form class="fb" method="post" action="{{ url_for('email_feedback', pid=p.id, action='verify') }}"><button class="ghost" title="Check with your verification service, re-guessing until valid">check</button></form>{% endif %}{% endif %}
{% elif p.email_status == 'failed' %}<span class="chip st-failed">no match after {{ p.email_attempts }} tries</span>
{% else %}<form class="fb" method="post" action="{{ url_for('email_feedback', pid=p.id, action='guess') }}"><button class="ghost">guess email</button></form>{% endif %}
{% endmacro %}"""

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zoomollo</title>
<script>
/* Theme + layout preferences: applied before the page draws, so there's no flash. */
(function () {
  var d = document.documentElement, mem = {}, mq = window.matchMedia("(max-width: 760px)");
  function get(k) { if (mem[k]) return mem[k]; try { return localStorage.getItem("zm-" + k) || "auto"; } catch (e) { return "auto"; } }
  window.zmSet = function (k, v) { mem[k] = v; try { localStorage.setItem("zm-" + k, v); } catch (e) {} window.zmApply(); };
  window.zmApply = function () {
    var t = get("theme"), l = get("layout");
    if (t === "auto") d.removeAttribute("data-theme"); else d.setAttribute("data-theme", t);
    // "Desktop" on a phone renders the full PC page, zoomed out.
    var vp = document.querySelector('meta[name="viewport"]');
    var narrow = Math.min(screen.width, window.innerWidth || screen.width) < 760;
    if (vp) vp.content = (l === "desktop" && narrow)
      ? "width=1200, initial-scale=" + (screen.width / 1200).toFixed(3)
      : "width=device-width, initial-scale=1";
    d.classList.toggle("mobile", l === "mobile" || (l === "auto" && mq.matches));
    d.classList.toggle("forced-mobile", l === "mobile" && !mq.matches);
    document.querySelectorAll("[data-set]").forEach(function (b) {
      b.setAttribute("aria-pressed", String(get(b.dataset.set) === b.dataset.val));
    });
  };
  window.zmApply();
  if (mq.addEventListener) mq.addEventListener("change", window.zmApply);
})();
</script>
<style>
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e4e4df;--accent:#2f5bea;--chip:#eef1fd;--hm:#fff4d6;--hmink:#7a5300;
--ok-bg:#dcfce7;--ok-ink:#166534;--bad-bg:#fee2e2;--bad-ink:#991b1b;--danger:#c2410c;--seg:#efefeb;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#141413;--card:#1e1e1c;--ink:#ececE8;--muted:#9a9a94;--line:#2e2e2b;--accent:#7d9bff;--chip:#232a45;--hm:#3a2f12;--hmink:#f3cd6d;
--ok-bg:#14361f;--ok-ink:#86efac;--bad-bg:#3f1515;--bad-ink:#fca5a5;--danger:#ea580c;--seg:#2a2a27;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#141413;--card:#1e1e1c;--ink:#ececE8;--muted:#9a9a94;--line:#2e2e2b;--accent:#7d9bff;--chip:#232a45;--hm:#3a2f12;--hmink:#f3cd6d;
--ok-bg:#14361f;--ok-ink:#86efac;--bad-bg:#3f1515;--bad-ink:#fca5a5;--danger:#ea580c;--seg:#2a2a27;color-scheme:dark}
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
form.fb{display:inline}form.fb button{padding:1px 7px;font-size:12px;border-radius:6px;margin-left:2px}
form.fb button.no{background:var(--danger)}form.fb button.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)}
.st-confirmed,.st-found{background:var(--ok-bg);color:var(--ok-ink)}.st-failed{background:var(--bad-bg);color:var(--bad-ink)}.st-catch_all{background:var(--hm);color:var(--hmink)}
/* Theme / layout switches */
.toggles{display:flex;gap:10px;margin-left:auto;flex-wrap:wrap}
.seg{display:inline-flex;align-items:center;background:var(--seg);border:1px solid var(--line);border-radius:10px;padding:3px;gap:2px}
.seg-l{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);padding:0 6px 0 6px}
.seg button{background:transparent;color:var(--ink);border:none;border-radius:7px;padding:6px 10px;font-size:13px;white-space:nowrap}
.seg button[aria-pressed="true"]{background:var(--accent);color:#fff;font-weight:600}
.seg button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
/* Mobile layout: stacked cards, bigger tap targets */
html.mobile header{flex-direction:column;flex-wrap:nowrap;align-items:stretch;gap:10px;padding:12px}
html.mobile .brand{display:flex;justify-content:space-between;align-items:center}
html.mobile nav{flex-wrap:nowrap;overflow-x:auto;gap:4px;margin:0 -12px;padding:0 12px;scrollbar-width:none}
html.mobile nav a{padding:8px 12px;border-radius:20px;background:var(--seg);white-space:nowrap}
html.mobile nav a.on{background:var(--accent);color:#fff}
html.mobile .toggles{margin-left:0;display:grid;grid-template-columns:minmax(0,1fr);gap:6px}
html.mobile .seg{display:flex;min-width:0}html.mobile .seg button{flex:1 1 0;min-width:0;padding:8px 2px;overflow:hidden;text-overflow:ellipsis}
html.mobile .seg-l{width:58px;flex:none}
html.mobile main{padding:12px}
html.mobile .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}html.mobile .stat{min-width:0;padding:8px 10px}
html.mobile form.bar > *{flex:1 1 100%}html.mobile form.bar button,html.mobile form.bar a.btn{flex:1 1 40%;text-align:center}
html.mobile input,html.mobile select,html.mobile textarea{font-size:16px}
html.mobile .grid,html.mobile .org{grid-template-columns:1fr}
html.mobile .wrap{background:none;border:none;overflow:visible}
html.mobile table,html.mobile tbody,html.mobile tr,html.mobile td{display:block;width:100%}
html.mobile tr.hdr{display:none}
html.mobile tr{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 14px;margin-bottom:10px}
html.mobile td{border:none;padding:4px 0;white-space:normal;word-break:break-word;min-width:0}
html.mobile td[data-label]::before{content:attr(data-label);display:block;font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
html.mobile td[data-label=""]::before{display:none}
html.mobile td.empty{display:none}
html.mobile td.wraptext{min-width:0}
html.mobile form.fb button{padding:6px 12px;font-size:14px;margin:4px 4px 0 0}
html.mobile .sub{font-size:14px}html.mobile h2{font-size:21px}
html.forced-mobile body{max-width:480px;margin:0 auto;border-left:1px solid var(--line);border-right:1px solid var(--line);min-height:100vh}
.reason{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
a{color:var(--accent)}.warn{background:var(--hm);color:var(--hmink);padding:10px 12px;border-radius:8px;font-size:13px;margin-bottom:12px}
</style></head><body>
<header><div class="brand"><b>Zoomollo</b></div><nav>
{% for t,label in [('find','Prospect'),('people','People'),('companies','Companies'),('contacts','Contacts'),('jobs','Jobs')] %}
<a href="{{ url_for(t) }}" class="{{ 'on' if tab==t }}">{{ label }}</a>{% endfor %}</nav>
<div class="toggles">
<div class="seg" role="group" aria-label="Theme"><span class="seg-l">Theme</span>
<button type="button" data-set="theme" data-val="light" onclick="zmSet('theme','light')">☀︎ Light</button>
<button type="button" data-set="theme" data-val="auto" onclick="zmSet('theme','auto')">Auto</button>
<button type="button" data-set="theme" data-val="dark" onclick="zmSet('theme','dark')">☾ Dark</button></div>
<div class="seg" role="group" aria-label="Layout"><span class="seg-l">Layout</span>
<button type="button" data-set="layout" data-val="desktop" onclick="zmSet('layout','desktop')">🖥 PC</button>
<button type="button" data-set="layout" data-val="auto" onclick="zmSet('layout','auto')">Auto</button>
<button type="button" data-set="layout" data-val="mobile" onclick="zmSet('layout','mobile')">📱 Mobile</button></div>
</div></header>
<main>
<div class="stats">{% for k,v in stats.items() %}<div class="stat"><span>{{ k }}</span><strong>{{ v }}</strong></div>{% endfor %}
{% if busy %}<div class="stat"><span>queue</span><strong>{{ busy }} running</strong></div>{% endif %}</div>
{{ body|safe }}
</main>
<script>
// Label every table cell with its column name so rows can stack as cards on mobile.
document.querySelectorAll("table").forEach(function (t) {
  var first = t.rows[0], heads = [];
  if (first && first.querySelector("th")) {
    first.classList.add("hdr");
    heads = Array.prototype.map.call(first.cells, function (c) { return c.textContent.trim(); });
  }
  Array.prototype.forEach.call(t.rows, function (r) {
    if (r === first) return;
    Array.prototype.forEach.call(r.cells, function (c, i) {
      c.setAttribute("data-label", c.colSpan > 1 ? "" : (heads[i] || ""));
      if (!c.textContent.trim() && !c.querySelector("form, input, img")) c.classList.add("empty");
    });
  });
});
window.zmApply();
</script></body></html>"""

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
<form class="card" method="post" action="{{ url_for('enqueue') }}"><h3>Bounced emails</h3>
<p>Paste bounce notices, a CSV, or addresses. Each bad address is blocked and that person gets the next guess (up to 10).</p>
<input type="hidden" name="action" value="bounces"><textarea name="items" placeholder="Delivery to jane.doe@acme.com failed…"></textarea><button>Process bounces</button>
{% if bounced %}<div class="log" style="margin-top:10px">{{ bounced|join('\n') }}</div>{% endif %}</form>
<form class="card" method="post" action="{{ url_for('enqueue') }}"><h3>Guess a work email</h3>
<input type="hidden" name="action" value="guess"><input name="name" placeholder="Jane Doe" style="width:100%;margin-bottom:8px">
<input name="domain" placeholder="acme.com" style="width:100%;margin-bottom:8px"><button>Guess</button>
{% if guesses %}<div class="log" style="margin-top:10px">{{ guesses|join('\\n') }}</div>{% endif %}</form>
</div>"""

PEOPLE = EMAIL_MACRO + """<form class="bar"><input type="search" name="q" value="{{ q }}" placeholder="Name, title, company, city, industry…">
<select name="department"><option value="">All departments</option>{% for k,v in depts.items() %}<option value="{{ k }}" {{ 'selected' if k==dept }}>{{ v }}</option>{% endfor %}</select>
<label class="chip" style="padding:8px 10px"><input type="checkbox" name="hiring" value="1" {{ 'checked' if hiring }} style="padding:0"> Hiring managers</label>
<button>Search</button><a class="btn" href="{{ url_for('export_people', q=q, department=dept, hiring=hiring) }}">Export CSV</a></form>
<div class="wrap"><table><tr><th>Name</th><th>Title</th><th>Company</th><th>Email</th><th>Phone</th><th>Hiring for</th><th></th></tr>
{% for p in rows %}<tr><td><b>{{ p.name }}</b></td><td>{{ p.title or '' }}</td>
<td><a href="{{ url_for('company', cid=p.company_id) }}">{{ p.company_name or p.company_key }}</a></td>
<td class="v">{{ email_cell(p, verify) }}</td>
<td class="v">{{ p.phone or '' }}</td><td class="wraptext">{% if p.hiring_for %}<span class="chip hm">{{ p.hiring_for }}</span>{% endif %}</td>
<td>{% if p.linkedin_url %}<a href="{{ p.linkedin_url }}" target="_blank" rel="noopener">profile</a>{% endif %}</td></tr>
{% else %}<tr><td colspan="7">No people yet. Start on the <a href="{{ url_for('find') }}">Prospect</a> tab.</td></tr>{% endfor %}</table></div>"""

COMPANY = EMAIL_MACRO + """<h2>{{ co.name or co.key }}</h2>
<form class="fb" method="post" action="{{ url_for('block', cid=co.id) }}" style="float:right"
 onsubmit="return confirm('Block this company as staffing/recruiting? Everything saved for it is deleted.')">
<button class="no">Block (staffing / recruiting)</button></form>
<div class="sub">{% if co.domain %}<a href="https://{{ co.domain }}" target="_blank" rel="noopener">{{ co.domain }}</a> · {% endif %}
{% if co.industry %}{{ co.industry }} · {% endif %}{% if co.city %}{{ co.city }} · {% endif %}
{% if co.phone %}<span class="v">{{ co.phone }}</span> · {% endif %}{% if co.address %}{{ co.address }} · {% endif %}
{% if co.email_pattern %}email format <span class="v">{{ co.email_pattern }}@{{ co.domain }}</span> ({{ (co.pattern_confidence*100)|int }}%){% endif %}</div>

<div class="section"><b>Org map</b> <span class="muted">· {{ people|length }} people</span></div>
{% if people %}<div class="org">{% for d, ps in org %}<div class="card dept"><h4>{{ depts[d] }}</h4>
{% for p in ps %}<div class="person"><div class="n">{{ p.name }} {% if p.hiring_for %}<span class="chip hm">hiring</span>{% endif %}</div>
<div class="t">{{ p.title or '' }}{% if p.seniority %} · {{ levels[p.seniority] }}{% endif %}</div>
<div class="c v">{{ email_cell(p, verify) }}
{% if p.phone %}<br>{{ p.phone }}{% endif %}{% if p.linkedin_url %}<br><a href="{{ p.linkedin_url }}" target="_blank" rel="noopener">profile</a>{% endif %}</div>
</div>{% endfor %}</div>{% endfor %}</div>
{% else %}<p class="muted">No people found yet.</p>{% endif %}

<div class="section"><b>Open roles × contacts</b> <span class="muted">· {{ jobs|length }}</span></div>
<div class="wrap"><table><tr><th>Role</th><th>Location</th><th>Source</th><th>Matched contacts</th></tr>
{% for j in jobs %}<tr><td>{% if j.url %}<a href="{{ j.url }}" target="_blank" rel="noopener">{{ j.title }}</a>{% else %}{{ j.title }}{% endif %}</td>
<td>{{ j.location or '' }}</td><td><span class="chip">{{ j.board }}</span></td><td class="wraptext">{% for m in matches.get(j.id, []) %}
<div><span class="reason">{{ m.reason }}</span> {% if m.name %}{{ m.name }} <span class="muted">({{ m.title or '' }})</span>{% endif %}
<span class="v">{{ m.email or m.value or '' }}</span></div>{% else %}<span class="muted">none yet</span>{% endfor %}</td></tr>
{% else %}<tr><td colspan="4">No open roles found.</td></tr>{% endfor %}</table></div>

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
<select name="kind"><option value="">Any kind</option>{% for k in ['person','company','guessed'] %}<option {{ 'selected' if k==kind }}>{{ k }}</option>{% endfor %}</select>
<button>Search</button><a class="btn" href="{{ url_for('export', q=q, type=type_, kind=kind) }}">Export CSV</a></form>
<div class="wrap"><table><tr><th>Company</th><th>Name</th><th>Title</th><th>Contact</th><th>Kind</th><th>Source</th></tr>
{% for r in rows %}<tr><td><a href="{{ url_for('company', cid=r.company_id) }}">{{ r.company_name or r.company_key or '' }}</a></td><td>{{ r.name or '' }}</td><td>{{ r.title or '' }}</td>
<td class="v">{{ r.value }}</td><td><span class="chip">{{ r.kind }}</span></td>
<td>{% if r.source_url and r.source_url.startswith('http') %}<a href="{{ r.source_url }}" target="_blank" rel="noopener">page</a>{% endif %}</td></tr>
{% else %}<tr><td colspan="6">Nothing yet.</td></tr>{% endfor %}</table></div>"""

JOBS = """<form class="bar"><input type="search" name="q" value="{{ q }}" placeholder="Role, company, city, contact…"><button>Search</button>
<a class="btn" href="{{ url_for('export_hiring', q=q) }}">Export roles × contacts CSV</a></form>
<div class="wrap"><table><tr><th>Company</th><th>Role</th><th>Location</th><th>Source</th><th>Best contacts</th></tr>
{% for j in rows %}<tr><td><a href="{{ url_for('company', cid=j.company_id) }}">{{ j.company_name or j.company_key }}</a></td>
<td>{% if j.url %}<a href="{{ j.url }}" target="_blank" rel="noopener">{{ j.title }}</a>{% else %}{{ j.title }}{% endif %}</td>
<td>{{ j.location or '' }}</td><td><span class="chip">{{ j.board }}</span></td>
<td class="wraptext">{% for m in matches.get(j.id, [])[:3] %}<div><span class="reason">{{ m.reason }}</span> {{ m.name or '' }}
<span class="v">{{ m.email or m.value or '' }}</span></div>{% else %}<span class="muted">none yet</span>{% endfor %}</td></tr>
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
                                   f"{r['hiring_managers']} hiring managers, {r['jobs']} jobs "
                                   f"({r['skipped_staffing']} staffing/recruiting firms skipped)")
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
        from .emailcheck import get_verifier
        body = render_template_string(tpl, depts=DEPT_LABELS, levels=LEVEL_LABELS,
                                      verify=get_verifier() is not None, **ctx)
        return render_template_string(PAGE, tab=tab, body=body, stats=db.stats(),
                                      busy=state["busy"] + tasks.qsize())

    @app.get("/")
    def find():
        return render("find", FIND, log=list(log), guesses=request.args.getlist("g"), bounced=request.args.getlist("b"),
                      provider=get_provider() is not None)

    @app.post("/")
    def enqueue():
        action = request.form.get("action")
        if action == "guess":
            name, domain = request.form.get("name", ""), request.form.get("domain", "")
            guesses = pipeline.guess_for_person(DB(db_path), name, domain) if name and domain else []
            return redirect(url_for("find", g=guesses[:5]))
        if action == "bounces":
            from .emailcheck import import_bounces
            res = import_bounces(DB(db_path), request.form.get("items", ""))
            lines = [f"✗ {b} ({n}) → {x or 'no guesses left'}" for b, n, x in res] or ["No saved addresses found in that text."]
            return redirect(url_for("find", b=lines))
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
        contacts = [r for r in db.search(limit=10_000) if r["company_id"] == cid and r["kind"] == "company"]
        matches = {j["id"]: db.job_contacts(j["id"]) for j in jobs}
        return render("companies", COMPANY, co=co, people=ppl, org=org, jobs=jobs,
                      matches=matches, contacts=contacts)

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
        db = DB(db_path)
        rows = db.jobs(q)
        return render("jobs", JOBS, rows=rows, q=q, matches={j["id"]: db.job_contacts(j["id"]) for j in rows})

    @app.get("/export.csv")
    def export():
        data = pipeline.export_csv(DB(db_path), request.args.get("q", ""),
                                   request.args.get("type") or None, request.args.get("kind") or None)
        return Response(data, mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=zoomollo-contacts.csv"})

    @app.post("/person/<int:pid>/email/<action>")
    def email_feedback(pid: int, action: str):
        from . import emailcheck
        db = DB(db_path)
        row = db.conn.execute("SELECT * FROM people WHERE id = ?", (pid,)).fetchone()
        if not row:
            abort(404)
        if action == "good" and row["email"]:
            emailcheck.mark_good(db, row["email"])
        elif action == "bad" and row["email"]:
            emailcheck.mark_bad(db, row["email"], "marked wrong")
        elif action == "guess":
            emailcheck.next_guess(db, pid)
        elif action == "verify" and (v := emailcheck.get_verifier()):
            emailcheck.auto_verify(db, pid, v)
        pipeline.assign_hiring_managers(db, row["company_id"])
        return redirect(request.referrer or url_for("people"))

    @app.post("/company/<int:cid>/block")
    def block(cid: int):
        DB(db_path).block_company(cid, "blocked by user")
        return redirect(url_for("companies"))

    @app.get("/hiring.csv")
    def export_hiring():
        return Response(pipeline.export_hiring_csv(DB(db_path), request.args.get("q", "")), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=zoomollo-roles-contacts.csv"})

    @app.get("/people.csv")
    def export_people():
        data = pipeline.export_people_csv(DB(db_path), request.args.get("q", ""),
                                          request.args.get("department") or None,
                                          request.args.get("hiring") in ("1", "True", "true"))
        return Response(data, mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=zoomollo-people.csv"})

    return app
