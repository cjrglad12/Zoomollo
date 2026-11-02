"""Web UI: prospect by industry + city, browse org maps, people, contacts and jobs."""

from __future__ import annotations

import os
import queue
import threading
from collections import deque

from flask import Flask, Response, abort, redirect, render_template_string, request, url_for

from . import pipeline
from .db import DB

DEPT_LABELS = {
    "executive": "Leadership", "sales": "Sales", "marketing": "Marketing", "operations": "Operations",
    "engineering": "Engineering", "product": "Product & Design", "finance": "Finance",
    "hr": "HR & Recruiting", "customer": "Customer", "it": "IT", "legal": "Legal", "other": "Other",
}
LEVEL_LABELS = {"c_level": "Exec", "vp": "VP", "director": "Director", "manager": "Manager", "staff": "Staff"}

EMAIL_MACRO = """{% macro email_cell(p) %}
{% if p.email %}<a href="mailto:{{ p.email }}">{{ p.email }}</a>
<span class="chip st-{{ p.email_status }}">{{ p.email_status }}{% if p.email_status == 'guessed' %} · try {{ (p.email_attempts or 0) + 1 }}/10{% endif %}</span>
{% if p.email_status != 'confirmed' %}<form class="fb" method="post" action="{{ url_for('email_feedback', pid=p.id, action='good') }}"><button title="Correct">✓</button></form>
<form class="fb" method="post" action="{{ url_for('email_feedback', pid=p.id, action='bad') }}"><button class="no" title="Wrong / bounced: try the next guess">✗</button></form>{% endif %}
{% elif p.email_status == 'failed' %}<span class="chip st-failed">no match after {{ p.email_attempts }} tries</span>
{% else %}<form class="fb" method="post" action="{{ url_for('email_feedback', pid=p.id, action='guess') }}"><button class="ghost">guess email</button></form>{% endif %}
{% endmacro %}"""

LINKEDIN_MACRO = """{% macro linkedin(p) %}<div class="listack">
{% if p.linkedin_url %}<a class="btn li" href="{{ p.linkedin_url }}" target="_blank" rel="noopener">LinkedIn</a>
{% else %}<a class="btn li" href="{{ li_search(p.name, p.company_name) }}" target="_blank" rel="noopener" title="Opens LinkedIn's people search in your browser">Find on LinkedIn</a>
<details class="lisave"><summary>save link</summary><form method="post" action="{{ url_for('save_linkedin', pid=p.id) }}">
<input name="url" placeholder="linkedin.com/in/…" required><button>Save</button></form></details>{% endif %}
</div>{% endmacro %}"""

SCORE_MACRO = """{% macro scorechip(sc) %}<span class="score s{{ 3 if sc.total >= 60 else 2 if sc.total >= 30 else 1 if sc.total > 0 else 0 }}" title="Hiring score">{{ sc.total }}</span>{% endmacro %}"""

OL_MACRO = """{% macro olbadge(st) %}{% if st and st.level %}<span class="chip ol-{{ st.level }}" title="{{ st.reason }}">{{ {'blocked': '⛔ Off-limits', 'warn': 'Past client', 'info': 'In Loxo'}[st.level] }}</span>{% endif %}{% endmacro %}"""

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
header{display:flex;gap:14px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line);background:var(--card);flex-wrap:wrap}
header b{font-size:18px}nav{display:flex;gap:11px;flex-wrap:wrap}nav a{color:var(--muted);text-decoration:none}nav a.on{color:var(--ink);font-weight:600}
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
form.fb button.no{background:var(--danger)}button.ghost,form.fb button.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)}
.st-confirmed,.st-found{background:var(--ok-bg);color:var(--ok-ink)}.st-failed{background:var(--bad-bg);color:var(--bad-ink)}.st-catch_all{background:var(--hm);color:var(--hmink)}
/* Theme / layout switches */
.toggles{display:flex;gap:8px;margin-left:auto;flex-wrap:wrap}
.seg{display:inline-flex;align-items:center;background:var(--seg);border:1px solid var(--line);border-radius:10px;padding:3px;gap:2px}
.seg-l{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);padding:0 6px 0 6px}
.seg button{background:transparent;color:var(--ink);border:none;border-radius:7px;padding:6px 8px;font-size:13px;white-space:nowrap}
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
html.mobile .tpl textarea{min-height:220px}html.mobile .sub{font-size:14px}html.mobile h2{font-size:21px}
html.forced-mobile body{max-width:480px;margin:0 auto;border-left:1px solid var(--line);border-right:1px solid var(--line);min-height:100vh}
td.em{white-space:normal;min-width:230px;max-width:300px}.score{display:inline-block;min-width:38px;text-align:center;font-weight:700;border-radius:8px;padding:3px 6px;background:var(--chip)}
.score.s3{background:var(--ok-bg);color:var(--ok-ink)}.score.s2{background:var(--hm);color:var(--hmink)}.score.s0{opacity:.55}
.why{font-size:13px;color:var(--muted)}.why b{color:var(--ink);font-weight:600}.fit{background:var(--ok-bg);color:var(--ok-ink)}
.ol-blocked{background:var(--bad-bg);color:var(--bad-ink)}.ol-warn{background:var(--hm);color:var(--hmink)}
.board{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(230px,1fr);gap:12px;overflow-x:auto;padding-bottom:6px}
.col h4{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.pcard{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:8px;font-size:14px}
.pcard select{padding:3px 6px;font-size:12px;margin-top:6px}.overdue{color:var(--bad-ink);font-weight:600}
html.mobile .board{grid-auto-flow:row;grid-auto-columns:auto}
.tl{border-left:2px solid var(--line);padding-left:12px}.tl div{margin-bottom:10px;white-space:pre-wrap;font-size:13px}
.tl .k{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.acts button{margin:2px 4px 2px 0}.steps td{padding:4px 6px}.steps input,.steps select{padding:4px 6px;font-size:13px}
a.btn.li{padding:3px 9px;font-size:12px;border-radius:6px;white-space:nowrap}
.listack{display:flex;flex-direction:column;align-items:flex-start;gap:4px}
details.lisave{font-size:12px}details.lisave summary{cursor:pointer;color:var(--accent)}
details.lisave form{display:flex;flex-direction:column;align-items:flex-start;gap:4px;margin-top:4px}details.lisave input{padding:4px 6px;font-size:12px;width:125px}
details.lisave button{padding:4px 8px;font-size:12px}.qr{background:#fff;display:inline-block;padding:8px;border-radius:8px;border:1px solid var(--line)}
a.msg{text-decoration:none;padding:1px 6px;border:1px solid var(--line);border-radius:6px;font-size:13px}
.sent{opacity:.55}.tpl textarea{width:100%;min-height:110px}.fields code{background:var(--chip);border-radius:4px;padding:1px 5px;font-size:12px;margin:2px;display:inline-block}
.preview{white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;font-size:14px}
.reason{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
a{color:var(--accent)}.warn{background:var(--hm);color:var(--hmink);padding:10px 12px;border-radius:8px;font-size:13px;margin-bottom:12px}
</style></head><body>
<header><div class="brand"><b>Zoomollo</b></div><nav>
{% for t,label in [('find','Prospect'),('changes','This week'),('pipeline_page','Pipeline'),('people','People'),('companies','Companies'),('contacts','Contacts'),('jobs','Jobs'),('messages','Messages')] %}
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
<p>Companies come from map data, chamber of commerce and association member directories, license and permit records on your city's and state's open-data portals (found automatically), your own public data sources below, and links between company websites. No search engines or paid services.</p>
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
<div class="card"><h3>Public data sources</h3>
<p>License lists, permit records or business registrations from government websites (a CSV/JSON link, or a file path on this computer). Every prospect run reads them and keeps the businesses that match the industry and city.</p>
{% for src in sources %}<div class="row" style="align-items:center;margin-bottom:6px"><span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis"><b>{{ src.label }}</b><br><span class="muted v" style="font-size:12px">{{ src.url }}</span></span>
<form class="fb" method="post" action="{{ url_for('enqueue') }}"><input type="hidden" name="action" value="source-remove"><input type="hidden" name="id" value="{{ src.id }}"><button class="no" title="Remove">✗</button></form></div>
{% else %}<p class="muted">None yet.</p>{% endfor %}
<form method="post" action="{{ url_for('enqueue') }}"><input type="hidden" name="action" value="source-add">
<input name="url" placeholder="https://data.yourcity.gov/resource/xxxx-xxxx.json" style="width:100%;margin-bottom:8px" required>
<input name="label" placeholder="Name (e.g. Austin building permits)" style="width:100%;margin-bottom:8px"><button>Add source</button></form></div>
<form class="card" method="post" action="{{ url_for('enqueue') }}"><h3>Bounced emails</h3>
<p>Paste bounce notices, a CSV, or addresses. Each bad address is blocked and that person gets the next guess (up to 10).</p>
<input type="hidden" name="action" value="bounces"><textarea name="items" placeholder="Delivery to jane.doe@acme.com failed…"></textarea><button>Process bounces</button>
{% if bounced %}<div class="log" style="margin-top:10px">{{ bounced|join('\n') }}</div>{% endif %}</form>
<form class="card" method="post" action="{{ url_for('enqueue') }}"><h3>Guess a work email</h3>
<input type="hidden" name="action" value="guess"><input name="name" placeholder="Jane Doe" style="width:100%;margin-bottom:8px">
<input name="domain" placeholder="acme.com" style="width:100%;margin-bottom:8px"><button>Guess</button>
{% if guesses %}<div class="log" style="margin-top:10px">{{ guesses|join('\\n') }}</div>{% endif %}</form>
</div>"""

PEOPLE = EMAIL_MACRO + LINKEDIN_MACRO + OL_MACRO + """<form class="bar"><input type="search" name="q" value="{{ q }}" placeholder="Name, title, company, city, industry…">
<select name="department"><option value="">All departments</option>{% for k,v in depts.items() %}<option value="{{ k }}" {{ 'selected' if k==dept }}>{{ v }}</option>{% endfor %}</select>
<label class="chip" style="padding:8px 10px"><input type="checkbox" name="hiring" value="1" {{ 'checked' if hiring }} style="padding:0"> Hiring managers</label>
<button>Search</button><a class="btn" href="{{ url_for('export_people', q=q, department=dept, hiring=hiring) }}">Export CSV</a></form>
{% if rows and sequences %}<form class="bar" method="post" action="{{ url_for('enroll_bulk') }}">
<input type="hidden" name="q" value="{{ q }}"><input type="hidden" name="department" value="{{ dept or '' }}"><input type="hidden" name="hiring" value="{{ '1' if hiring else '' }}">
<span class="muted" style="align-self:center">Start follow-ups for everyone shown:</span>
<select name="sequence">{% for sq in sequences %}<option value="{{ sq.id }}">{{ sq.name }}</option>{% endfor %}</select>
<button title="Skips off-limits companies and people without an email or phone">Start</button></form>{% endif %}
<div class="wrap"><table><tr><th>Name</th><th>Title</th><th>Company</th><th>Email</th><th>Phone</th><th>Hiring for</th><th></th></tr>
{% for p in rows %}<tr><td><b>{{ p.name }}</b></td><td>{{ p.title or '' }}</td>
<td><a href="{{ url_for('company', cid=p.company_id) }}">{{ p.company_name or p.company_key }}</a> {{ olbadge(ol(p.company_id)) }}</td>
<td class="v em">{{ email_cell(p) }}</td>
<td class="v">{% if p.phone %}{{ p.phone }} <a class="msg" href="{{ url_for('message', person=p.id) }}" title="Send a text">💬</a>{% endif %}</td><td class="wraptext">{% if p.hiring_for %}<span class="chip hm">{{ p.hiring_for }}</span>{% endif %}</td>
<td>{{ linkedin(p) }}{% if (p.email or p.phone) and sequences and not ol(p.company_id).blocked %}
<form class="fb" method="post" action="{{ url_for('enroll') }}"><input type="hidden" name="person" value="{{ p.id }}">
<input type="hidden" name="sequence" value="{{ sequences[0].id }}"><button class="ghost" style="background:transparent;color:var(--accent);border:1px solid var(--line);margin-top:4px" title="Start: {{ sequences[0].name }}">Follow up</button></form>{% endif %}</td></tr>
{% else %}<tr><td colspan="7">No people yet. Start on the <a href="{{ url_for('find') }}">Prospect</a> tab.</td></tr>{% endfor %}</table></div>"""

COMPANY = EMAIL_MACRO + LINKEDIN_MACRO + OL_MACRO + SCORE_MACRO + """<h2>{{ co.name or co.key }}</h2>
<form class="fb" method="post" action="{{ url_for('block', cid=co.id) }}" style="float:right"
 onsubmit="return confirm('Block this company as staffing/recruiting? Everything saved for it is deleted.')">
<button class="no">Block (staffing / recruiting)</button></form>
<div class="sub">{% if co.domain %}<a href="https://{{ co.domain }}" target="_blank" rel="noopener">{{ co.domain }}</a> · {% endif %}
{% if co.industry %}{{ co.industry }} · {% endif %}{% if co.city %}{{ co.city }} · {% endif %}
{% if co.phone %}<span class="v">{{ co.phone }}</span> <a class="msg" href="{{ url_for('message', company=co.id) }}" title="Send a text">💬</a> · {% endif %}{% if co.address %}{{ co.address }} · {% endif %}
{% if co.email_pattern %}email format <span class="v">{{ co.email_pattern }}@{{ co.domain }}</span> ({{ ((co.pattern_confidence or 0)*100)|int }}%){% endif %}</div>
{% if offl.level %}<div class="{{ 'warn' if offl.level != 'info' else 'preview' }}" style="margin-bottom:12px">{{ olbadge(offl) }} {{ offl.reason }}</div>{% endif %}
<div class="grid">
<div class="card"><h3>Pipeline</h3>
{% if card %}
<form method="post" action="{{ url_for('pipeline_post', cid=co.id) }}" class="row" style="margin-bottom:8px"><input type="hidden" name="action" value="update">
<select name="stage">{% for k, v in stages %}<option value="{{ k }}" {{ 'selected' if k == card.stage }}>{{ v }}</option>{% endfor %}</select>
<select name="contact"><option value="">Main contact…</option>{% for p in people %}<option value="{{ p.id }}" {{ 'selected' if p.id == card.contact_id }}>{{ p.name }}{% if p.title %} ({{ p.title }}){% endif %}</option>{% endfor %}</select>
<input name="next_step" value="{{ card.next_step or '' }}" placeholder="Next step, e.g. call Tuesday" style="flex:2;min-width:160px">
<input type="date" name="due_date" value="{{ card.due_date or '' }}">
<input name="owner" value="{{ card.owner or '' }}" placeholder="Owner" style="max-width:110px"><button>Save</button></form>
<form method="post" action="{{ url_for('pipeline_post', cid=co.id) }}" class="row"><input type="hidden" name="action" value="note">
<input name="note" placeholder="Add a note (call outcome, fee terms…)" style="flex:1" required><button>Add note</button></form>
{% else %}
<p>Not in your pipeline yet.</p>
<form method="post" action="{{ url_for('pipeline_post', cid=co.id) }}" class="row"><input type="hidden" name="action" value="add">
<select name="contact"><option value="">Main contact…</option>{% for p in people %}<option value="{{ p.id }}">{{ p.name }}{% if p.title %} ({{ p.title }}){% endif %}</option>{% endfor %}</select>
{% if offl.blocked %}<label class="chip" style="padding:8px 10px"><input type="checkbox" name="force" value="1" required style="padding:0"> Add anyway ({{ offl.reason }})</label>{% endif %}
<button>Add to pipeline</button></form>{% endif %}
{% if sequences %}<form method="post" action="{{ url_for('enroll') }}" class="row" style="margin-top:10px">
<select name="person">{% for p in people if p.email or p.phone %}<option value="{{ p.id }}" {{ 'selected' if card and p.id == card.contact_id }}>{{ p.name }}</option>{% endfor %}</select>
<select name="sequence">{% for sq in sequences %}<option value="{{ sq.id }}">{{ sq.name }}</option>{% endfor %}</select>
{% if offl.blocked %}<label class="chip" style="padding:8px 10px"><input type="checkbox" name="force" value="1" required style="padding:0"> Override off-limits</label>{% endif %}
<button>Start follow-ups</button></form>{% endif %}
{% for e in enrollments %}<div class="row" style="align-items:center;margin-top:6px;font-size:13px"><span style="flex:1"><b>{{ e.name }}</b> · {{ e.sequence_name }} · <span class="chip">{{ e.status }}</span>{% if e.status == 'active' %} · step {{ e.step_no }}, due {{ e.next_due }}{% endif %}</span>
{% if e.status == 'active' %}<form class="fb" method="post" action="{{ url_for('enrollment_post', eid=e.id) }}"><button name="action" value="stop" class="no">Stop</button></form>{% endif %}</div>{% endfor %}
{% if timeline %}<div class="section"><b>Timeline</b></div><div class="tl">{% for a in timeline %}<div><span class="k">{{ a.created_at[:16].replace('T', ' ') }} · {{ a.kind }}{% if a.person_name %} · {{ a.person_name }}{% endif %}</span><br>{{ a.body }}</div>{% endfor %}</div>{% endif %}
</div>
<div class="card"><h3>Off-limits</h3>
{% if co.offlimits_reason %}<p>Marked by you: <b>{{ co.offlimits_reason }}</b>{% if co.offlimits_until %} until {{ co.offlimits_until }}{% endif %}.</p>
<form method="post" action="{{ url_for('offlimits_post', cid=co.id) }}"><input type="hidden" name="action" value="clear"><button>Clear</button></form>
{% else %}<form method="post" action="{{ url_for('offlimits_post', cid=co.id) }}"><input type="hidden" name="action" value="set">
<input name="reason" placeholder="Reason, e.g. client of a partner agency" style="width:100%;margin-bottom:8px" required>
<label class="muted" style="font-size:13px">Until (optional) <input type="date" name="until"></label><br><button style="margin-top:8px">Mark off-limits</button></form>{% endif %}
<p style="margin-top:12px">{% if loxo_row %}In Loxo as <b>{{ loxo_row.name }}</b>: {{ loxo_row.open_jobs }} open job(s), {{ loxo_row.all_jobs }} total, {{ loxo_row.placements }} placement(s){% if loxo_row.last_placement %}, last {{ loxo_row.last_placement }}{% endif %}.
{% elif loxo_on %}Not found in Loxo{% if loxo_synced %} (synced {{ loxo_synced[:10] }}){% else %} (not synced yet: see the Pipeline tab){% endif %}.{% else %}Loxo isn't connected, so only your own off-limits marks apply.{% endif %}</p>
</div></div>

<div class="section"><b>Hiring score</b></div>
<div class="card" style="display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap"><div style="font-size:30px">{{ scorechip(score) }}</div>
<div style="flex:1;min-width:220px">{% for pts, why in score.reasons %}<div class="why"><b>{{ '%+d' % pts if pts else '·' }}</b> {{ why }}</div>{% else %}<div class="why">No open roles found yet.</div>{% endfor %}
{% if not desk_set %}<div class="why" style="margin-top:6px">Tip: set <a href="{{ url_for('companies') }}">My desk</a> so only roles you place count.</div>{% endif %}</div></div>

<div class="section"><b>Org map</b> <span class="muted">· {{ people|length }} people</span></div>
{% if people %}<div class="org">{% for d, ps in org %}<div class="card dept"><h4>{{ depts[d] }}</h4>
{% for p in ps %}<div class="person"><div class="n">{{ p.name }} {% if p.hiring_for %}<span class="chip hm">hiring</span>{% endif %}</div>
<div class="t">{{ p.title or '' }}{% if p.seniority %} · {{ levels[p.seniority] }}{% endif %}</div>
<div class="c v">{{ email_cell(p) }}
{% if p.phone %}<br>{{ p.phone }} <a class="msg" href="{{ url_for('message', person=p.id) }}" title="Send a text">💬</a>{% endif %}<br>{{ linkedin(dict(p, company_name=co.name or co.key)) }}</div>
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

DESK_CARD = """<details class="card" {{ 'open' if not desk.is_set }} style="margin-bottom:14px"><summary><b>My desk</b>
<span class="muted">{% if desk.is_set %}· {{ desk.titles|join(', ') or 'any role' }}{% if desk.locations %} · {{ desk.locations|join('; ') }}{% endif %}{% if desk.min_salary %} · ${{ '{:,}'.format(desk.min_salary) }}+{% endif %}{% else %}· set your niche so the score focuses on roles you place{% endif %}</span></summary>
<form method="post" action="{{ url_for('desk_post') }}" style="margin-top:10px"><div class="grid">
<label>Roles you place <span class="muted">(comma or one per line)</span><textarea name="titles" style="width:100%;min-height:70px" placeholder="HVAC technician, service manager, estimator">{{ desk.titles|join(', ') }}</textarea></label>
<label>Roles to skip<textarea name="exclude" style="width:100%;min-height:70px" placeholder="intern, driver">{{ desk.exclude|join(', ') }}</textarea></label>
<label>Locations you cover <span class="muted">(blank = anywhere)</span><textarea name="locations" style="width:100%;min-height:70px" placeholder="Austin, TX; Dallas, TX; MO; remote">{{ desk.locations|join('; ') }}</textarea></label>
<label>Minimum salary <span class="muted">(only used when a post states pay)</span><input name="min_salary" value="{{ desk.min_salary or '' }}" placeholder="60000" style="width:100%"></label>
</div><button style="margin-top:10px">Save my desk</button></form></details>"""

COMPANIES = SCORE_MACRO + OL_MACRO + """{{ desk_card|safe }}
<form class="bar"><input type="search" name="q" value="{{ q }}" placeholder="Company, city, industry…">
<label class="chip" style="padding:8px 10px"><input type="checkbox" name="niche" value="1" {{ 'checked' if niche }} style="padding:0"> Only with roles in my niche</label><button>Search</button></form>
<div class="wrap"><table><tr><th>Score</th><th>Company</th><th>Why</th><th>Roles</th><th>People</th><th>Stage</th></tr>
{% for sc, c in rows %}<tr><td>{{ scorechip(sc) }}</td>
<td><a href="{{ url_for('company', cid=c.id) }}"><b>{{ c.name or c.key }}</b></a>{% if c.city %}<br><span class="muted">{{ c.city }}</span>{% endif %}</td>
<td class="wraptext why">{% if sc.offlimits.blocked %}{{ olbadge(sc.offlimits) }} {{ sc.offlimits.reason }}{% else %}{{ sc.top_reasons|join(' · ') or 'no open roles found' }}{% endif %}</td>
<td>{% if sc.fit_roles %}<span class="chip fit">{{ sc.fit_roles|length }} fit</span>{% endif %}{% if sc.other_roles %} <span class="chip">{{ sc.other_roles }} other</span>{% endif %}</td>
<td>{{ counts.get(c.id, 0) }}</td>
<td>{% if sc.stage %}<span class="chip">{{ stage_names[sc.stage] }}</span>{% elif not sc.offlimits.blocked and sc.total %}<form class="fb" method="post" action="{{ url_for('pipeline_post', cid=c.id) }}"><input type="hidden" name="action" value="add"><button class="ghost">+ Pipeline</button></form>{% endif %}</td></tr>
{% else %}<tr><td colspan="6">{% if niche %}No companies with open roles in your niche yet.{% else %}No companies yet.{% endif %}</td></tr>{% endfor %}</table></div>"""

CONTACTS = """<form class="bar"><input type="search" name="q" value="{{ q }}" placeholder="Search name, email, title, company…">
<select name="type"><option value="">Emails + phones</option>{% for t in ['email','phone'] %}<option {{ 'selected' if t==type_ }}>{{ t }}</option>{% endfor %}</select>
<select name="kind"><option value="">Any kind</option>{% for k in ['person','company','guessed'] %}<option {{ 'selected' if k==kind }}>{{ k }}</option>{% endfor %}</select>
<button>Search</button><a class="btn" href="{{ url_for('export', q=q, type=type_, kind=kind) }}">Export CSV</a></form>
<div class="wrap"><table><tr><th>Company</th><th>Name</th><th>Title</th><th>Contact</th><th>Kind</th><th>Source</th></tr>
{% for r in rows %}<tr><td><a href="{{ url_for('company', cid=r.company_id) }}">{{ r.company_name or r.company_key or '' }}</a></td><td>{{ r.name or '' }}</td><td>{{ r.title or '' }}</td>
<td class="v">{{ r.value }}</td><td><span class="chip">{{ r.kind }}</span></td>
<td>{% if r.source_url and r.source_url.startswith('http') %}<a href="{{ r.source_url }}" target="_blank" rel="noopener">page</a>{% endif %}</td></tr>
{% else %}<tr><td colspan="6">Nothing yet.</td></tr>{% endfor %}</table></div>"""

MESSAGES = """<div class="grid">
<form class="card" method="post" action="{{ url_for('messages_post') }}"><h3>Your details</h3>
<p>Used for {my_name} and {my_company} in templates.</p><input type="hidden" name="action" value="me">
<input name="my_name" value="{{ my_name }}" placeholder="Your name" style="width:100%;margin-bottom:8px">
<input name="my_company" value="{{ my_company }}" placeholder="Your company" style="width:100%;margin-bottom:8px">
<input name="my_address" value="{{ my_address }}" placeholder="Business mailing address (required in sales emails)" style="width:100%;margin-bottom:8px"><button>Save</button>
{% if not my_address %}<p class="muted" style="margin-top:8px">US law (CAN-SPAM) requires a mailing address in sales emails; it's added to the email footer.</p>{% endif %}</form>

<form class="card" method="post" action="{{ url_for('messages_post') }}"><h3>Draft a template with AI</h3>
<p>Runs on your own computer (Ollama) — nothing is sent anywhere. {% if not ai %}<b>Not set up yet:</b> {{ ai_help }}{% endif %}</p>
<input type="hidden" name="action" value="draft">
<input name="goal" value="{{ goal }}" placeholder="Goal, e.g. offer a pre-vetted candidate for their open role" style="width:100%;margin-bottom:8px">
<div class="row"><select name="tone">{% for t in ['warm and direct','formal','casual and upbeat','short and to the point'] %}<option {{ 'selected' if t==tone }}>{{ t }}</option>{% endfor %}</select>
<select name="length"><option value="short">Short</option><option value="medium" {{ 'selected' if length=='medium' }}>Medium</option></select>
<button>Draft</button></div>{% if draft_error %}<div class="warn" style="margin-top:10px">{{ draft_error }}</div>{% endif %}</form>

<div class="card big"><h3>Templates</h3>
<p class="fields">Fields: {% for f in fields %}<code>{{ '{' ~ f ~ '}' }}</code>{% endfor %} — <code>{role}</code> is the open role the person most likely owns.</p>
{% for t in templates %}<form class="tpl" method="post" action="{{ url_for('messages_post') }}" style="margin-bottom:14px">
<input type="hidden" name="action" value="save"><input type="hidden" name="id" value="{{ t.id }}">
<input name="name" value="{{ t.name }}" style="width:100%;margin-bottom:6px;font-weight:600"><textarea name="body">{{ t.body }}</textarea>
<div class="row"><button>Save</button><a class="btn" href="{{ url_for('messages', template=t.id) }}#queue">Use for outreach</a>
<button name="action" value="delete" class="no" style="background:var(--danger)" onclick="return confirm('Delete this template?')">Delete</button></div></form>{% endfor %}
<form class="tpl" method="post" action="{{ url_for('messages_post') }}"><input type="hidden" name="action" value="save">
<input name="name" value="{{ 'AI draft' if draft else '' }}" placeholder="New template name" style="width:100%;margin-bottom:6px;font-weight:600" required>
<textarea name="body" placeholder="Hi {first_name}, ..." required>{{ draft or '' }}</textarea><button>{{ 'Save AI draft' if draft else 'Add template' }}</button></form></div>

<div class="card big" id="email-templates"><h3>Email templates</h3>
<p>Same fields as texts. Your name, company, mailing address and an unsubscribe line are added to the bottom of every email.</p>
{% for t in email_templates %}<form class="tpl" method="post" action="{{ url_for('messages_post') }}" style="margin-bottom:14px">
<input type="hidden" name="action" value="save"><input type="hidden" name="kind" value="email"><input type="hidden" name="id" value="{{ t.id }}">
<input name="name" value="{{ t.name }}" style="width:100%;margin-bottom:6px;font-weight:600">
<input name="subject" value="{{ t.subject or '' }}" placeholder="Subject" style="width:100%;margin-bottom:6px"><textarea name="body">{{ t.body }}</textarea>
<div class="row"><button>Save</button><button name="action" value="delete" class="no" style="background:var(--danger)" onclick="return confirm('Delete this template?')">Delete</button></div></form>{% endfor %}
<form class="tpl" method="post" action="{{ url_for('messages_post') }}"><input type="hidden" name="action" value="save"><input type="hidden" name="kind" value="email">
<input name="name" placeholder="New email template name" style="width:100%;margin-bottom:6px;font-weight:600" required>
<input name="subject" placeholder="Subject, e.g. {role} at {company}" style="width:100%;margin-bottom:6px" required>
<textarea name="body" placeholder="Hi {first_name}, ..." required></textarea><button>Add email template</button></form></div>

<div class="card big" id="sequences"><h3>Follow-up sequences</h3>
<p>Steps go out this many days after someone is started. Text steps are skipped for people without a phone, email steps for people without an email. A reply, or moving the company past <i>Contacted</i>, stops the sequence.</p>
{% for sq in sequences %}<form method="post" action="{{ url_for('messages_post') }}" style="margin-bottom:16px"><input type="hidden" name="action" value="sequence"><input type="hidden" name="id" value="{{ sq.id }}">
<input name="name" value="{{ sq.name }}" style="font-weight:600;margin-bottom:6px;width:100%">
<table class="steps">{% for st in sq.steps + [None] %}<tr><td>Day <input type="number" name="day" value="{{ st.day if st else '' }}" min="0" style="width:64px"></td>
<td><select name="channel"><option value="email" {{ 'selected' if st and st.channel == 'email' }}>Email</option><option value="text" {{ 'selected' if st and st.channel == 'text' }}>Text</option></select></td>
<td><select name="template"><option value="">{{ 'Template…' if st else '+ add a step' }}</option>{% for t in all_templates %}<option value="{{ t.id }}" {{ 'selected' if st and st.template_id == t.id }}>{{ 'Email' if t.kind == 'email' else 'Text' }}: {{ t.name }}</option>{% endfor %}</select></td></tr>{% endfor %}</table>
<div class="row" style="margin-top:6px"><button>Save sequence</button><span class="muted" style="align-self:center;font-size:12px">Clear a step's day to remove it.</span></div></form>{% endfor %}
<form method="post" action="{{ url_for('messages_post') }}" class="row"><input type="hidden" name="action" value="sequence">
<input name="name" placeholder="New sequence name" required><button>Create sequence</button></form></div>

<div class="card big" id="queue"><h3>Outreach queue</h3>
<p>People with their own phone number who haven't been texted yet. <b>Text</b> opens your texting app with the message written (on a Mac: Messages, sending from your iPhone) — you press Send. <b>Phone / edit</b> shows a QR code to send from your phone instead.</p>
<form class="bar" action="{{ url_for('messages') }}#queue"><select name="template">{% for t in templates %}<option value="{{ t.id }}" {{ 'selected' if t.id==tid }}>{{ t.name }}</option>{% endfor %}</select>
<input type="search" name="q" value="{{ q }}" placeholder="Filter: company, city, title…">
<label class="chip" style="padding:8px 10px"><input type="checkbox" name="all" value="1" {{ 'checked' if all_people }} style="padding:0"> Everyone (not just hiring managers)</label><button>Show</button></form>
<div class="wrap"><table><tr><th>Person</th><th>Message</th><th>Send</th></tr>
{% for r in queue %}<tr id="q{{ r.p.id }}"><td><b>{{ r.p.name }}</b><br><span class="muted">{{ r.p.title or '' }} · {{ r.p.company_name or '' }}</span><br><span class="v">{{ r.p.phone }}</span></td>
<td class="wraptext"><div class="preview">{{ r.text }}</div>{% if r.missing %}<span class="muted" style="font-size:12px">Not known, filled generically: {{ r.missing|join(', ') }}</span>{% endif %}</td>
<td>{{ send_buttons(r.p.phone, r.text, r.p.id, None, tid) }}<br><a href="{{ url_for('message', person=r.p.id, template=tid) }}">phone / edit</a></td></tr>
{% else %}<tr><td colspan="3">{% if not templates %}Add a template first.{% else %}Nobody to text: no saved people with their own phone number (company main lines are on each company page).{% endif %}</td></tr>{% endfor %}</table></div></div>
</div>"""

SEND_MACRO = """{% macro send_buttons(phone, text, pid, cid, tid) %}
<form class="fb" method="post" action="{{ url_for('message_send') }}" onsubmit="var r=this.closest('tr');if(r)r.classList.add('sent')">
<input type="hidden" name="phone" value="{{ phone }}"><input type="hidden" name="text" value="{{ text }}">
<input type="hidden" name="channel" value="sms"><input type="hidden" name="person" value="{{ pid or '' }}">
<input type="hidden" name="company" value="{{ cid or '' }}"><input type="hidden" name="template" value="{{ tid or '' }}">
<button title="Opens your texting app (Messages on a Mac)">Text</button></form>
{% endmacro %}"""

MESSAGE_ONE = """<h2>Text {{ who }}</h2><div class="sub">{{ phone }}{% if last %} · last texted {{ last[:10] }}{% endif %}</div>
{% if blocked %}<div class="warn">This number opted out. It can't be texted from Zoomollo.</div>{% else %}
<form class="bar" action="{{ url_for('message') }}"><input type="hidden" name="person" value="{{ pid or '' }}"><input type="hidden" name="company" value="{{ cid or '' }}">
<select name="template" onchange="this.form.submit()">{% for t in templates %}<option value="{{ t.id }}" {{ 'selected' if t.id==tid }}>{{ t.name }}</option>{% endfor %}</select></form>
<div class="grid">
<form method="post" action="{{ url_for('message_send') }}" class="card tpl" id="msgform">
<textarea name="text" id="msgtext" oninput="zmQR()">{{ text }}</textarea>
{% if missing %}<p>Not known, filled generically: {{ missing|join(', ') }}. Edit the message if it reads oddly.</p>{% endif %}
<input type="hidden" name="phone" value="{{ phone }}"><input type="hidden" name="person" value="{{ pid or '' }}">
<input type="hidden" name="company" value="{{ cid or '' }}"><input type="hidden" name="template" value="{{ tid or '' }}">
{% if offl and offl.blocked %}<div class="warn">{{ offl.reason }}. <label><input type="checkbox" name="override" value="1" required style="padding:0"> Send anyway</label></div>{% endif %}
<div class="row"><button name="channel" value="sms" title="Opens your texting app. On a Mac this is Messages, sending from your iPhone">Open in Messages</button>
<button type="button" class="ghost" style="background:transparent;color:var(--accent);border:1px solid var(--line)" onclick="zmCopy(this)">Copy text</button>
<button name="channel" value="manual" class="ghost" style="background:transparent;color:var(--accent);border:1px solid var(--line)" title="Log it after sending from your phone">Mark as sent</button></div></form>
<div class="card"><h3>Send from your phone</h3><p>Scan with your phone's camera: your texting app opens with this message ready. Then tap <b>Mark as sent</b>.</p>
<div class="qr" id="qr">{{ qr|safe }}</div></div></div>
<form method="post" action="{{ url_for('message_optout') }}" style="margin-top:12px" onsubmit="return confirm('Mark this number as opted out? It will never be texted again.')">
<input type="hidden" name="phone" value="{{ phone }}"><button class="no" style="background:var(--danger)">They opted out (STOP)</button></form>
<script>
function zmCopy(b){var t=document.getElementById("msgtext").value;
  (navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(function(){b.textContent="Copied ✓"},function(){var a=document.getElementById("msgtext");a.select();document.execCommand("copy");b.textContent="Copied ✓"});}
var zmT;function zmQR(){clearTimeout(zmT);zmT=setTimeout(function(){
  fetch("{{ url_for('message_qr') }}",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},
   body:"phone="+encodeURIComponent({{ phone|tojson }})+"&text="+encodeURIComponent(document.getElementById("msgtext").value)})
  .then(function(r){return r.text()}).then(function(svg){document.getElementById("qr").innerHTML=svg});},400);}
</script>{% endif %}
{% if history %}<div class="section"><b>Sent before</b></div>{% for m in history %}<div class="preview" style="margin-bottom:8px"><span class="muted">{{ m.sent_at[:16].replace('T',' ') }} · {{ m.channel }}</span><br>{{ m.body }}</div>{% endfor %}{% endif %}"""

PIPELINE = OL_MACRO + SCORE_MACRO + """<div class="grid">
<div class="card" id="email"><h3>Email</h3>
{% if email_msg %}<div class="{{ 'warn' if email_bad else 'preview' }}" style="margin-bottom:10px">{{ email_msg }}</div>{% endif %}
{% if smtp %}<p>Sending from your mailbox (<b>{{ smtp_user }}</b>). {{ sent_today }} of {{ daily_limit }} sent today.</p>
<form method="post" action="{{ url_for('email_setup') }}" class="row">
{% if due_emails %}<button formaction="{{ url_for('pipeline_tools') }}" name="action" value="send_all">Send all {{ due_emails }} due emails</button>{% endif %}
<button name="action" value="test_send" class="ghost">Send test email to myself</button>
{% if gmail %}<button name="action" value="disconnect" class="ghost" onclick="return confirm('Remove your Gmail details from this computer?')">Disconnect</button>{% endif %}</form>
{% else %}<p><b>Open in email app</b> works with no setup. To send from Zoomollo and have replies and bounces picked up automatically, connect Gmail:</p>
<form method="post" action="{{ url_for('email_setup') }}"><input type="hidden" name="action" value="gmail">
<input type="email" name="address" placeholder="you@gmail.com or your Google Workspace address" style="width:100%;margin-bottom:8px" required>
<input type="password" name="app_password" placeholder="Google app password (16 letters)" autocomplete="off" style="width:100%;margin-bottom:8px" required>
<button>Connect Gmail</button></form>
<details style="font-size:13px;margin-top:8px"><summary>How to get an app password</summary><ol style="padding-left:18px;margin:6px 0">
<li>Turn on 2-Step Verification in your Google Account (Security).</li>
<li>Open <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noopener">myaccount.google.com/apppasswords</a>, name it "Zoomollo", and create it.</li>
<li>Paste the 16-letter password here. It's saved only in zoomollo-settings.txt on this computer.</li>
<li>For reply checking, IMAP must be on: Gmail → Settings → Forwarding and POP/IMAP.</li></ol>
Workspace accounts: your admin may need to allow app passwords. Microsoft 365: use <b>Open in email app</b>.</details>{% endif %}</div>
<div class="card"><h3>Replies &amp; bounces</h3>{% if imap %}<p>Your inbox is checked every 10 minutes{% if inbox_at %} (last {{ inbox_at[11:16] }}){% endif %}. Replies stop follow-ups, “unsubscribe” blocks the address, bounces try the next email guess.</p>
<form method="post" action="{{ url_for('pipeline_tools') }}"><input type="hidden" name="action" value="inbox"><button>Check now</button></form>
{% else %}<p>Mark replies with the stage menu below, or <b>connect Gmail</b> (Email box) and Zoomollo spots replies, unsubscribes and bounces in your inbox for you.</p>{% endif %}</div>
<div class="card"><h3>Loxo off-limits</h3>{% if loxo_on %}<p>{{ loxo_count }} Loxo companies{% if loxo_synced %}, synced {{ loxo_synced[:16].replace('T', ' ') }}{% else %}, not synced yet{% endif %}. Active clients and recent placements are blocked from outreach.</p>
<form method="post" action="{{ url_for('pipeline_tools') }}" class="row"><button name="action" value="loxo">Quick sync</button>
<button name="action" value="loxo_full" class="ghost" title="All companies, statuses and block flags; takes several minutes on big accounts">Full sync</button></form>
<p class="muted" style="font-size:12px;margin-top:6px">Runs by itself: jobs and placements every 6 hours, everything weekly{% if loxo_full %} (last full {{ loxo_full[:10] }}){% endif %}.</p>
{% else %}<p>Add LOXO_AGENCY_SLUG and LOXO_API_KEY to <b>zoomollo-settings.txt</b> so active clients and recent placements are blocked automatically. Until then, mark companies off-limits on their page.</p>{% endif %}</div>
</div>
{% if log %}<div class="log card" style="margin-top:12px">{{ log|join('\n') }}</div>{% endif %}

{% if pipe_changes %}<div class="section"><b>Changes at your pipeline companies</b> <span class="muted">· last 7 days · <a href="{{ url_for('changes', mine=1) }}">all</a></span></div>
<div class="wrap"><table><tr><th>Company</th><th>What changed</th><th>When</th></tr>{% for e in pipe_changes[:8] %}<tr>
<td><a href="{{ url_for('company', cid=e.company_id) }}">{{ e.company_name or e.company_key }}</a></td><td class="wraptext">{{ e.detail }}</td><td>{{ e.created_at[:10] }}</td></tr>{% endfor %}</table></div>{% endif %}
{% if best %}<div class="section"><b>Best accounts to work next</b> <span class="muted">· highest hiring score, not in your pipeline</span></div>
<div class="wrap"><table><tr><th>Score</th><th>Company</th><th>Why</th><th></th></tr>
{% for sc, c in best %}<tr><td>{{ scorechip(sc) }}</td><td><a href="{{ url_for('company', cid=c.id) }}"><b>{{ c.name or c.key }}</b></a></td>
<td class="wraptext why">{{ sc.top_reasons|join(' · ') }}</td>
<td><form class="fb" method="post" action="{{ url_for('pipeline_post', cid=c.id) }}"><input type="hidden" name="action" value="add"><button>+ Pipeline</button></form></td></tr>{% endfor %}</table></div>{% endif %}

<div class="section"><b>Due today</b> <span class="muted">· {{ due|length }}</span></div>
<div class="wrap"><table><tr><th>Who</th><th>What</th><th></th></tr>
{% for it in due %}{% if it.kind == 'task' %}<tr><td><a href="{{ url_for('company', cid=it.company_id) }}">{{ it.company_name }}</a></td>
<td class="wraptext"><span class="reason">next step{% if it.due < today %} · <span class="overdue">overdue</span>{% endif %}</span><br>{{ it.next_step }} <span class="muted">(due {{ it.due }})</span></td>
<td><form class="fb" method="post" action="{{ url_for('pipeline_post', cid=it.company_id) }}"><input type="hidden" name="action" value="task_done"><button>Done</button></form></td></tr>
{% else %}<tr><td><b>{{ it.name }}</b><br><span class="muted">{{ it.title or '' }}</span><br><a href="{{ url_for('company', cid=it.company_id) }}">{{ it.company_name }}</a></td>
<td class="wraptext"><form method="post" action="{{ url_for('due_post', eid=it.enrollment_id) }}" id="d{{ it.enrollment_id }}">
<span class="reason">step {{ it.step_no }} · {{ it.channel }} to {{ it.to }}{% if it.due < today %} · <span class="overdue">overdue</span>{% endif %}</span>
{% if it.channel == 'email' %}<input name="subject" value="{{ it.subject }}" style="width:100%;margin:4px 0">{% endif %}
<textarea name="body" style="width:100%;min-height:{{ 150 if it.channel == 'email' else 80 }}px">{{ it.body }}</textarea>
{% if it.missing %}<span class="muted" style="font-size:12px">Filled generically: {{ it.missing|join(', ') }}</span>{% endif %}
<div class="acts">{% if it.channel == 'email' %}{% if smtp %}<button name="action" value="smtp">Send</button>{% endif %}
<button name="action" value="mailto" class="{{ 'ghost' if smtp }}">Open in email app</button>
{% else %}<button name="action" value="text">Text</button><button name="action" value="phone" title="Sent it from your phone">Mark sent</button>{% endif %}
<button name="action" value="skip" class="ghost">Skip step</button>
<button name="action" value="stop" class="no" style="background:var(--danger)">Stop</button></div></form></td><td></td></tr>{% endif %}
{% else %}<tr><td colspan="3">Nothing due. Start follow-ups from the People tab or a company page.</td></tr>{% endfor %}</table></div>

<div class="section"><b>Pipeline</b> <span class="muted">· {{ total }} companies</span></div>
<div class="board">{% for k, label in stages %}{% if board[k] or k in ('prospect', 'contacted', 'replied', 'meeting') %}<div class="col"><h4>{{ label }} · {{ board[k]|length }}</h4>
{% for c in board[k] %}<div class="pcard"><a href="{{ url_for('company', cid=c.company_id) }}"><b>{{ c.company_name or c.company_key }}</b></a> {{ olbadge(c.offlimits) }}
{% if c.contact_name %}<br><span class="muted">{{ c.contact_name }}{% if c.contact_title %}, {{ c.contact_title }}{% endif %}</span>{% endif %}
{% if c.next_step %}<br><span class="{{ 'overdue' if c.overdue }}">{{ c.next_step }}{% if c.due_date %} · {{ c.due_date }}{% endif %}</span>{% endif %}
<form method="post" action="{{ url_for('pipeline_post', cid=c.company_id) }}"><input type="hidden" name="action" value="stage">
<select name="stage" onchange="this.form.submit()">{% for k2, l2 in stages %}<option value="{{ k2 }}" {{ 'selected' if k2 == k }}>{{ l2 }}</option>{% endfor %}</select></form></div>
{% else %}<p class="muted" style="font-size:13px">—</p>{% endfor %}</div>{% endif %}{% endfor %}</div>"""

CHANGES = SCORE_MACRO + """<form class="bar">
<select name="days" onchange="this.form.submit()">{% for d in [7, 14, 30, 90] %}<option value="{{ d }}" {{ 'selected' if d == days }}>Last {{ d }} days</option>{% endfor %}</select>
<label class="chip" style="padding:8px 10px"><input type="checkbox" name="niche" value="1" {{ 'checked' if niche }} style="padding:0" onchange="this.form.submit()"> Only my niche</label>
<label class="chip" style="padding:8px 10px"><input type="checkbox" name="mine" value="1" {{ 'checked' if mine }} style="padding:0" onchange="this.form.submit()"> Only my pipeline</label></form>
<div class="card" style="margin-bottom:14px"><p style="margin:0">Companies are re-checked every {{ every }} days while Zoomollo is open (up to {{ per_hour }} an hour). {{ due_count }} due now{% if last_run %}; last re-check {{ last_run[:16].replace('T', ' ') }}{% endif %}.</p>
<form method="post" action="{{ url_for('recheck_now') }}" style="margin-top:8px"><button {{ 'disabled' if not due_count }}>Re-check {{ [due_count, per_hour]|min }} now</button></form></div>
{% for kind, label in kinds.items() %}<div class="section"><b>{{ label }}</b> <span class="muted">· {{ groups[kind]|length }}</span>
{% if kind == 'job_reposted' %}<span class="muted"> · strongest signal: they couldn't fill it</span>{% elif kind == 'job_gone' %}<span class="muted"> · ask who filled it, or if it's on hold</span>{% elif kind == 'person_new' %}<span class="muted"> · new managers often build their team</span>{% endif %}</div>
<div class="wrap"><table><tr><th>Score</th><th>Company</th><th>What changed</th><th>When</th><th></th></tr>
{% for e in groups[kind] %}<tr><td>{% if e.score %}{{ scorechip(e.score) }}{% endif %}</td>
<td><a href="{{ url_for('company', cid=e.company_id) }}"><b>{{ e.company_name or e.company_key }}</b></a>{% if e.city %}<br><span class="muted">{{ e.city }}</span>{% endif %}</td>
<td class="wraptext">{% if e.job_url %}<a href="{{ e.job_url }}" target="_blank" rel="noopener">{{ e.detail }}</a>{% else %}{{ e.detail }}{% endif %}{% if e.fit %} <span class="chip fit">fits my desk</span>{% endif %}</td>
<td>{{ e.created_at[:10] }}</td>
<td>{% if e.stage %}<span class="chip">{{ stage_names[e.stage] }}</span>{% elif e.score and not e.score.offlimits.blocked %}<form class="fb" method="post" action="{{ url_for('pipeline_post', cid=e.company_id) }}"><input type="hidden" name="action" value="add"><button class="ghost">+ Pipeline</button></form>{% endif %}</td></tr>
{% else %}<tr><td colspan="5" class="muted">None.</td></tr>{% endfor %}</table></div>{% endfor %}"""

JOBS = """<form class="bar"><input type="search" name="q" value="{{ q }}" placeholder="Role, company, city, contact…">
<label class="chip" style="padding:8px 10px"><input type="checkbox" name="niche" value="1" {{ 'checked' if niche }} style="padding:0"> Only my niche</label><button>Search</button>
<a class="btn" href="{{ url_for('export_hiring', q=q) }}">Export roles × contacts CSV</a></form>
<div class="wrap"><table><tr><th>Company</th><th>Role</th><th>Location</th><th>Source</th><th>Best contacts</th></tr>
{% for j in rows %}<tr><td><a href="{{ url_for('company', cid=j.company_id) }}">{{ j.company_name or j.company_key }}</a></td>
<td>{% if j.url %}<a href="{{ j.url }}" target="_blank" rel="noopener">{{ j.title }}</a>{% else %}{{ j.title }}{% endif %}
{% if fits[j.id][0] %} <span class="chip fit">fits my desk</span>{% elif desk_set %} <span class="muted" style="font-size:12px">{{ fits[j.id][1] }}</span>{% endif %}</td>
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
                if action == "inbox":
                    from . import crm
                    r = crm.check_inbox(db)
                    if r["reply"] or r["bounce"] or r["unsubscribe"] or opts.get("loud"):
                        log.appendleft(f"✓ Inbox: {r['reply']} replies, {r['unsubscribe']} unsubscribes, "
                                       f"{r['bounce']} bounces")
                elif action == "send_emails":
                    from . import crm
                    r = crm.send_due_emails(db, pause=8.0, progress=log.appendleft)
                    log.appendleft(f"✓ Sent {r['sent']} emails" + (f", {r['failed']} failed" if r["failed"] else ""))
                elif action == "prospect":
                    r = pipeline.prospect(db, opts["what"], opts["where"], opts.get("limit", 25),
                                          progress=log.appendleft)
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

    def inbox_timer():
        import time
        from . import crm
        while True:
            time.sleep(600)
            if crm.imap_configured():
                tasks.put(("inbox", "inbox", {}))

    if os.environ.get("ZOOMOLLO_NO_BACKGROUND") != "1":
        threading.Thread(target=inbox_timer, daemon=True).start()

    loxo_busy = threading.Lock()

    def run_loxo_sync(full: bool | None):
        """Loxo syncs get their own thread: a full sync of a big account takes minutes."""
        from . import loxo
        if not loxo_busy.acquire(blocking=False):
            log.appendleft("Loxo sync already running")
            return
        try:
            db = DB(db_path)
            r = loxo.auto_sync(db, progress=log.appendleft) if full is None else \
                loxo.sync(db, progress=log.appendleft, full=full)
            if r:
                log.appendleft(f"✓ Loxo {'full' if r['full'] else 'quick'} sync: {r['jobs']} jobs, "
                               f"{r['placements']} placements" + (f", {r['companies']} companies" if r["full"] else ""))
        except Exception as e:  # noqa: BLE001
            log.appendleft(f"✗ Loxo sync failed: {e}")
        finally:
            loxo_busy.release()

    def loxo_timer():
        import time
        from . import loxo
        time.sleep(5)
        while True:
            if loxo.configured():
                run_loxo_sync(None)
            time.sleep(3600)

    if os.environ.get("ZOOMOLLO_NO_BACKGROUND") != "1":
        threading.Thread(target=loxo_timer, daemon=True).start()

    recheck_busy = threading.Lock()

    def run_recheck():
        """Re-check companies that are due (own thread: each site takes a few seconds)."""
        from . import recheck
        if not recheck_busy.acquire(blocking=False):
            return
        try:
            db = DB(db_path)
            recheck.run(db, progress=log.appendleft)
            db.set_setting("recheck_at", __import__("datetime").datetime.now().isoformat(timespec="seconds"))
        except Exception as e:  # noqa: BLE001
            log.appendleft(f"✗ Re-check failed: {e}")
        finally:
            recheck_busy.release()

    def recheck_timer():
        import time
        time.sleep(60)
        while True:
            run_recheck()
            time.sleep(3600)

    if os.environ.get("ZOOMOLLO_NO_BACKGROUND") != "1":
        threading.Thread(target=recheck_timer, daemon=True).start()

    def render(tab, tpl, **ctx):
        db = DB(db_path)
        from .offlimits import status as offlimits_status
        from .orgmap import linkedin_search_url
        cache: dict = {}

        def ol(cid):
            if cid not in cache:
                cache[cid] = offlimits_status(db, cid)
            return cache[cid]
        body = render_template_string(tpl, depts=DEPT_LABELS, levels=LEVEL_LABELS, li_search=linkedin_search_url,
                                      dict=dict, ol=ol, **ctx)
        return render_template_string(PAGE, tab=tab, body=body, stats=db.stats(),
                                      busy=state["busy"] + tasks.qsize())

    @app.get("/")
    def find():
        return render("find", FIND, log=list(log), guesses=request.args.getlist("g"), bounced=request.args.getlist("b"),
                      sources=DB(db_path).sources())

    @app.post("/")
    def enqueue():
        action = request.form.get("action")
        if action == "guess":
            name, domain = request.form.get("name", ""), request.form.get("domain", "")
            guesses = pipeline.guess_for_person(DB(db_path), name, domain) if name and domain else []
            return redirect(url_for("find", g=guesses[:5]))
        if action == "source-add" and request.form.get("url", "").strip():
            DB(db_path).add_source(request.form["url"], request.form.get("label", "").strip() or None)
            return redirect(url_for("find"))
        if action == "source-remove":
            DB(db_path).remove_source(int(request.form.get("id", 0)))
            return redirect(url_for("find"))
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

    # --- messages -------------------------------------------------------------
    from . import messaging

    @app.get("/messages")
    def messages(**extra):
        db = DB(db_path)
        messaging.ensure_starters(db)
        templates = db.templates()
        tid = int(request.args.get("template") or (templates[0]["id"] if templates else 0))
        tpl = db.template(tid) if tid else None
        q = request.args.get("q", "")
        all_people = bool(request.args.get("all"))
        queue = []
        if tpl:
            for p in messaging.outreach_list(db, q=q, hiring_only=not all_people, limit=100):
                text, missing = messaging.render(tpl["body"], messaging.person_fields(db, p["id"]))
                queue.append({"p": p, "text": text, "missing": missing})
        from . import ai, crm
        crm.ensure_starters(db)
        return render("messages", SEND_MACRO + MESSAGES, templates=templates, tid=tid, queue=queue, q=q,
                      email_templates=db.templates("email"), all_templates=db.templates(None),
                      sequences=crm.sequences(db),
                      my_address=db.get_setting("my_address") or os.environ.get("MY_ADDRESS", ""),
                      all_people=all_people, fields=messaging.FIELDS, my_name=db.get_setting("my_name") or os.environ.get("MY_NAME", ""),
                      my_company=db.get_setting("my_company") or os.environ.get("MY_COMPANY", ""),
                      ai=ai.available(), ai_help=ai.SETUP_HELP,
                      draft=extra.get("draft"), draft_error=extra.get("draft_error"),
                      goal=extra.get("goal", ""), tone=extra.get("tone", "warm and direct"),
                      length=extra.get("length", "short"))

    @app.post("/messages")
    def messages_post():
        db = DB(db_path)
        action = request.form.get("action")
        if action == "me":
            db.set_setting("my_name", request.form.get("my_name", ""))
            db.set_setting("my_company", request.form.get("my_company", ""))
            db.set_setting("my_address", request.form.get("my_address", ""))
        elif action == "save":
            name, body = request.form.get("name", "").strip(), request.form.get("body", "").strip()
            kind = "email" if request.form.get("kind") == "email" else "text"
            if name and body:
                db.save_template(name, body, int(request.form.get("id") or 0) or None, kind=kind,
                                 subject=request.form.get("subject", "").strip() or None)
            if kind == "email":
                return redirect(url_for("messages") + "#email-templates")
        elif action == "sequence":
            from . import crm
            steps = []
            for day, ch, tid in zip(request.form.getlist("day"), request.form.getlist("channel"),
                                    request.form.getlist("template")):
                if day.strip() and tid:
                    steps.append((int(day), "text" if ch == "text" else "email", int(tid)))
            crm.save_sequence(db, request.form.get("name", "Sequence").strip() or "Sequence", steps,
                              int(request.form.get("id") or 0) or None)
            return redirect(url_for("messages") + "#sequences")
        elif action == "delete":
            db.delete_template(int(request.form.get("id") or 0))
        elif action == "draft":
            from . import ai
            goal, tone, length = (request.form.get(k, "") for k in ("goal", "tone", "length"))
            try:
                draft = ai.draft_template(goal, tone or "warm and direct", length or "short")
                return messages(draft=draft, goal=goal, tone=tone, length=length)
            except ai.AIUnavailable as e:
                return messages(draft_error=str(e), goal=goal, tone=tone, length=length)
            except Exception as e:  # noqa: BLE001
                return messages(draft_error=f"The AI couldn't draft that ({e}).", goal=goal, tone=tone,
                                length=length)
        return redirect(url_for("messages"))

    @app.get("/message")
    def message():
        db = DB(db_path)
        messaging.ensure_starters(db)
        pid = int(request.args.get("person") or 0) or None
        cid = int(request.args.get("company") or 0) or None
        if pid:
            p = db.conn.execute("SELECT p.*, co.name AS company_name FROM people p JOIN companies co "
                                "ON co.id = p.company_id WHERE p.id = ?", (pid,)).fetchone()
            if not p or not p["phone"]:
                abort(404)
            phone, who = p["phone"], f"{p['name']} ({p['company_name']})"
        elif cid:
            co = db.company_row(cid)
            if not co or not co["phone"]:
                abort(404)
            phone, who = co["phone"], f"{co['name'] or co['key']} (main line)"
        else:
            abort(404)
        templates = db.templates()
        tid = int(request.args.get("template") or (templates[0]["id"] if templates else 0))
        tpl = db.template(tid) if tid else None
        text, missing = (messaging.render(tpl["body"], messaging.person_fields(db, pid, cid)) if tpl else ("", []))
        e164 = "+" + messaging.digits(phone)
        history = db.conn.execute("SELECT * FROM messages WHERE phone = ? ORDER BY sent_at DESC", (e164,)).fetchall()
        return render("messages", MESSAGE_ONE, who=who, phone=phone, pid=pid, cid=cid, templates=templates,
                      tid=tid, text=text, missing=missing, history=history,
                      qr=messaging.qr_svg(messaging.sms_link(phone, text)),
                      offl=__import__("zoomollo.offlimits", fromlist=["status"]).status(
                          db, cid or (db.conn.execute("SELECT company_id FROM people WHERE id = ?", (pid,)).fetchone() or [None])[0]),
                      last=messaging.last_messaged(db, phone), blocked=db.is_suppressed(e164))

    @app.post("/message/send")
    def message_send():
        db = DB(db_path)
        f = request.form
        phone, text = f.get("phone", ""), f.get("text", "").strip()
        channel = "phone" if f.get("channel") == "manual" else "sms"
        from .offlimits import status as offlimits_status
        pid = int(f.get("person") or 0) or None
        cid = int(f.get("company") or 0) or None
        if pid and not cid:
            row = db.conn.execute("SELECT company_id FROM people WHERE id = ?", (pid,)).fetchone()
            cid = row["company_id"] if row else None
        st = offlimits_status(db, cid)
        if st.blocked and not f.get("override"):
            return Response(f"Not sent. {st.reason}. Open the person's text page to send anyway.", status=403)
        if not messaging.log_message(db, phone, text, channel, pid, cid, int(f.get("template") or 0) or None):
            return Response("This number opted out and can't be texted.", status=403)
        if cid:
            from . import crm
            crm.log(db, cid, "text", text, pid)
            crm.advance_to(db, cid, "contacted")
            db.commit()
        if f.get("channel") == "manual":  # sent from the phone via the QR code
            return redirect(request.referrer or url_for("messages"))
        return redirect(messaging.sms_link(phone, text))

    @app.post("/message/qr")
    def message_qr():
        f = request.form
        return messaging.qr_svg(messaging.sms_link(f.get("phone", ""), f.get("text", "")))

    @app.post("/person/<int:pid>/linkedin")
    def save_linkedin(pid: int):
        from .orgmap import clean_linkedin_url
        url = clean_linkedin_url(request.form.get("url", ""))
        if url:
            db = DB(db_path)
            db.conn.execute("UPDATE people SET linkedin_url = ? WHERE id = ?", (url, pid))
            db.commit()
        return redirect(request.referrer or url_for("people"))

    @app.post("/message/optout")
    def message_optout():
        phone = request.form.get("phone", "")
        DB(db_path).suppress("+" + messaging.digits(phone), "opted out (STOP)")
        return redirect(request.referrer or url_for("messages"))

    @app.get("/people")
    def people():
        q, d, h = request.args.get("q", ""), request.args.get("department") or None, bool(request.args.get("hiring"))
        from . import crm
        db = DB(db_path)
        crm.ensure_starters(db)
        return render("people", PEOPLE, rows=db.people(q=q, department=d, hiring_only=h, limit=1000),
                      q=q, dept=d, hiring=h, sequences=crm.sequences(db))

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
        from . import crm, loxo
        from .offlimits import loxo_match, status as offlimits_status
        crm.ensure_starters(db)
        enrollments = db.conn.execute(
            "SELECT e.*, p.name, s.name AS sequence_name FROM enrollments e JOIN people p ON p.id = e.person_id "
            "JOIN sequences s ON s.id = e.sequence_id WHERE e.company_id = ? ORDER BY e.id DESC", (cid,)).fetchall()
        from . import hiringscore
        desk = hiringscore.load_desk(db)
        return render("companies", COMPANY, co=co, people=ppl, org=org, jobs=jobs,
                      score=hiringscore.score_all(db, desk)[cid], desk_set=desk.is_set,
                      matches=matches, contacts=contacts, card=crm.card(db, cid), stages=crm.STAGES,
                      sequences=crm.sequences(db), enrollments=enrollments, timeline=crm.timeline(db, cid),
                      offl=offlimits_status(db, cid), loxo_row=loxo_match(db, cid), loxo_on=loxo.configured(),
                      loxo_synced=db.get_setting("loxo_synced_at"))

    # --- pipeline & follow-ups ------------------------------------------------------
    @app.get("/pipeline")
    def pipeline_page():
        import os
        from . import crm, loxo
        db = DB(db_path)
        crm.ensure_starters(db)
        due = crm.due_items(db)
        board = crm.board(db)
        from . import hiringscore
        best = [x for x in hiringscore.ranked(db, exclude_pipeline=True, limit=8) if x[0].total > 0]
        from .recheck import changes as recent_changes
        pipe_changes = sorted((e for g in recent_changes(db, 7, pipeline_only=True).values() for e in g),
                              key=lambda e: e["created_at"], reverse=True)
        return render("pipeline_page", PIPELINE, due=due, board=board, stages=crm.STAGES, today=crm.today(), best=best,
                      pipe_changes=pipe_changes,
                      total=sum(len(v) for v in board.values()), smtp=crm.smtp_configured(),
                      smtp_user=os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER"),
                      sent_today=crm.sent_today(db), daily_limit=crm.daily_limit(),
                      due_emails=sum(1 for i in due if i["kind"] == "step" and i["channel"] == "email"),
                      imap=crm.imap_configured(), inbox_at=db.get_setting("inbox_checked_at"),
                      gmail=bool(os.environ.get("GMAIL_ADDRESS")), email_msg=request.args.get("email_msg", ""),
                      email_bad=request.args.get("email_bad") == "1",
                      loxo_on=loxo.configured(), loxo_synced=db.get_setting("loxo_synced_at"),
                      loxo_full=db.get_setting("loxo_full_synced_at"),
                      loxo_count=db.conn.execute("SELECT COUNT(*) FROM loxo_companies").fetchone()[0],
                      log=[ln for ln in list(log)[:8] if any(w in ln for w in ("Loxo", "Inbox", "Sent", "✗", "✓ "))])

    @app.post("/email/setup")
    def email_setup():
        import os
        from . import crm
        from .settings import GMAIL, save_values
        f = request.form
        action = f.get("action")
        msg, bad = "", False
        if action == "gmail":
            addr = f.get("address", "").strip()
            pw = "".join(f.get("app_password", "").split())
            save_values({"GMAIL_ADDRESS": addr, "GMAIL_APP_PASSWORD": pw, **GMAIL,
                         "SMTP_USER": addr, "SMTP_PASSWORD": pw})
            problems = crm.test_connection()
            if problems:
                msg, bad = "Saved, but: " + " ".join(problems), True
            else:
                msg = "Gmail connected: sending works" + (" and replies will be picked up." if crm.imap_configured() else ".")
        elif action == "disconnect":
            save_values({k: "" for k in ["GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "SMTP_HOST", "SMTP_PORT", "SMTP_USER",
                                         "SMTP_PASSWORD", "IMAP_HOST", "IMAP_PORT"]})
            msg = "Gmail disconnected and removed from this computer."
        elif action == "test_send":
            me = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER", "")
            try:
                crm.send_smtp(DB(db_path), me, "Zoomollo test email",
                              "This is a test from Zoomollo. If you can read it, sending works.")
                msg = f"Test email sent to {me}. Check your inbox."
            except Exception as e:  # noqa: BLE001
                msg, bad = f"Test email failed: {e}", True
        return redirect(url_for("pipeline_page", email_msg=msg, email_bad=int(bad)) + "#email")

    @app.post("/pipeline/tools")
    def pipeline_tools():
        action = request.form.get("action")
        if action in ("loxo", "loxo_full"):
            full = action == "loxo_full"
            threading.Thread(target=run_loxo_sync, args=(full,), daemon=True).start()
            log.appendleft(f"✓ Loxo {'full' if full else 'quick'} sync started")
        elif action == "inbox":
            tasks.put(("inbox", "inbox", {"loud": True}))
            log.appendleft("queued: inbox check")
        elif action == "send_all":
            tasks.put(("send_emails", "emails", {}))
            log.appendleft("queued: sending due emails")
        return redirect(url_for("pipeline_page"))

    @app.post("/pipeline/<int:cid>")
    def pipeline_post(cid: int):
        from . import crm
        db = DB(db_path)
        f = request.form
        action = f.get("action")
        try:
            if action == "add":
                crm.add_to_pipeline(db, cid, int(f.get("contact") or 0) or None, force=bool(f.get("force")))
            elif action == "stage":
                crm.set_stage(db, cid, f.get("stage", "prospect"))
            elif action == "update":
                c = crm.card(db, cid)
                if c and f.get("stage") and f.get("stage") != c["stage"]:
                    crm.set_stage(db, cid, f["stage"])
                crm.update_card(db, cid, contact_id=int(f.get("contact") or 0) or None, owner=f.get("owner", ""),
                                next_step=f.get("next_step", "").strip(), due_date=f.get("due_date", ""))
            elif action == "note":
                crm.add_note(db, cid, f.get("note", ""))
            elif action == "task_done":
                crm.task_done(db, cid)
        except crm.OffLimits as e:
            return Response(f"Not added. {e}. Tick 'Add anyway' to override.", status=403)
        return redirect(request.referrer or url_for("company", cid=cid))

    @app.post("/enroll")
    def enroll():
        from . import crm
        db = DB(db_path)
        try:
            crm.enroll(db, int(request.form["person"]), int(request.form["sequence"]), force=bool(request.form.get("force")))
        except crm.OffLimits as e:
            return Response(f"Not started. {e}.", status=403)
        except ValueError as e:
            return Response(f"Not started. {e}.", status=400)
        return redirect(request.referrer or url_for("pipeline_page"))

    @app.post("/enroll/bulk")
    def enroll_bulk():
        from . import crm
        db = DB(db_path)
        f = request.form
        started = skipped = 0
        for p in db.people(q=f.get("q", ""), department=f.get("department") or None,
                           hiring_only=bool(f.get("hiring")), limit=1000):
            try:
                crm.enroll(db, p["id"], int(f["sequence"]))
                started += 1
            except (crm.OffLimits, ValueError):
                skipped += 1
        log.appendleft(f"✓ Follow-ups started for {started} people ({skipped} skipped: off-limits or no contact info)")
        return redirect(url_for("pipeline_page"))

    @app.post("/enrollment/<int:eid>")
    def enrollment_post(eid: int):
        from . import crm
        db = DB(db_path)
        if request.form.get("action") == "stop":
            crm.stop(db, eid)
        elif request.form.get("action") == "skip":
            crm.skip_step(db, eid)
        return redirect(request.referrer or url_for("pipeline_page"))

    @app.post("/due/<int:eid>")
    def due_post(eid: int):
        from . import crm
        db = DB(db_path)
        f = request.form
        action = f.get("action")
        item = next((i for i in crm.due_items(db) if i["kind"] == "step" and i["enrollment_id"] == eid), None)
        if action == "stop":
            crm.stop(db, eid)
        elif action == "skip":
            crm.skip_step(db, eid)
        elif item:
            subject, body = f.get("subject", item["subject"]), f.get("body", item["body"])
            if action == "smtp":
                try:
                    mid = crm.send_smtp(db, item["to"], subject, body)
                except Exception as e:  # noqa: BLE001
                    return Response(f"Couldn't send: {e}", status=502)
                crm.complete_step(db, eid, "smtp", subject, body, item["to"])
                db.conn.execute("UPDATE emails SET message_id = ? WHERE id = (SELECT MAX(id) FROM emails)", (mid,))
                db.commit()
            elif action == "mailto":
                crm.complete_step(db, eid, "mailto", subject, body, item["to"])
                return redirect(crm.mailto_link(item["to"], subject, body))
            elif action == "text":
                crm.complete_step(db, eid, "sms", body=body, to=item["to"])
                return redirect(messaging.sms_link(item["to"], body))
            elif action == "phone":
                crm.complete_step(db, eid, "phone", body=body, to=item["to"])
        return redirect(url_for("pipeline_page"))

    @app.post("/offlimits/<int:cid>")
    def offlimits_post(cid: int):
        from .offlimits import set_manual
        db = DB(db_path)
        if request.form.get("action") == "clear":
            set_manual(db, cid, None)
        else:
            set_manual(db, cid, request.form.get("reason", "").strip() or "marked by you",
                       request.form.get("until") or None)
        return redirect(url_for("company", cid=cid))

    def desk_card(db):
        from .hiringscore import load_desk
        return render_template_string(DESK_CARD, desk=load_desk(db))

    @app.get("/changes")
    def changes():
        from . import crm, recheck
        db = DB(db_path)
        days = int(request.args.get("days") or 7)
        niche, mine = bool(request.args.get("niche")), bool(request.args.get("mine"))
        return render("changes", CHANGES, groups=recheck.changes(db, days, niche, mine), kinds=recheck.KINDS,
                      days=days, niche=niche, mine=mine, every=recheck.every_days(), per_hour=recheck.per_hour(),
                      due_count=len(recheck.due(db, limit=100_000)), last_run=db.get_setting("recheck_at"),
                      stage_names=crm.STAGE_NAMES)

    @app.post("/changes/recheck")
    def recheck_now():
        threading.Thread(target=run_recheck, daemon=True).start()
        log.appendleft("✓ Re-check started")
        return redirect(url_for("changes"))

    @app.get("/companies")
    def companies():
        from . import crm, hiringscore
        db = DB(db_path)
        q, niche = request.args.get("q", ""), bool(request.args.get("niche"))
        rows = hiringscore.ranked(db, niche_only=niche, q=q)
        counts = {r[0]: r[1] for r in db.conn.execute("SELECT company_id, COUNT(*) FROM people GROUP BY company_id")}
        return render("companies", COMPANIES, rows=rows, q=q, niche=niche, counts=counts,
                      stage_names=crm.STAGE_NAMES, desk_card=desk_card(db))

    @app.post("/desk")
    def desk_post():
        from .hiringscore import save_desk
        f = request.form
        save_desk(DB(db_path), f.get("titles", ""), f.get("exclude", ""), f.get("locations", ""), f.get("min_salary"))
        return redirect(request.referrer or url_for("companies"))

    @app.get("/contacts")
    def contacts():
        q, t, k = request.args.get("q", ""), request.args.get("type") or None, request.args.get("kind") or None
        return render("contacts", CONTACTS, rows=DB(db_path).search(q, t, k), q=q, type_=t, kind=k)

    @app.get("/jobs")
    def jobs():
        q = request.args.get("q", "")
        db = DB(db_path)
        from .hiringscore import job_fit, load_desk
        desk = load_desk(db)
        niche = bool(request.args.get("niche"))
        cities = {r["id"]: r["city"] for r in db.conn.execute("SELECT id, city FROM companies")}
        rows = db.jobs(q)
        fits = {j["id"]: job_fit(j["title"], j["location"], cities.get(j["company_id"]), j["salary_max"], desk) for j in rows}
        if niche:
            rows = [j for j in rows if fits[j["id"]][0]]
        return render("jobs", JOBS, rows=rows, q=q, niche=niche, fits=fits, desk_set=desk.is_set,
                      matches={j["id"]: db.job_contacts(j["id"]) for j in rows})

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
