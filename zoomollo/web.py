"""Web UI: prospect by industry + city, browse org maps, people, contacts and jobs."""

from __future__ import annotations

import os
import queue
import re
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
{% if p.review_note %}<span class="chip" title="{{ p.review_note }}">needs review</span>{% endif %}
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

# Phone cell used on every list: the row's own number, else the company's main line, else "not found".
SUMMARY_MACRO = """{% macro cosummary(s) %}{% set bits = [s.summary, ('about ' ~ '{:,}'.format(s.headcount) ~ ' people') if s.headcount and s.headcount_source != 'apollo-range' else ('101–500 people' if s.headcount_source == 'apollo-range' else ''), s.city or ''] | select | list %}{% if bits %}<div class="zm-sum">{{ bits | join(' · ') }}</div>{% endif %}{% endmacro %}"""
PHONE_MACRO = SUMMARY_MACRO + """{% macro phonecell(own, main=None) %}{% if own %}<span class="v">{{ own|phone }}</span>{% elif main %}<span class="v">{{ main|phone }}</span><br><span class="muted" style="font-size:12px">main line</span>{% else %}<span class="muted">not found</span>{% endif %}{% endmacro %}"""

ROLES_MACRO = """{% macro roleslist(roles, show=5) %}{% if roles %}{% set total = roles|sum(attribute='n') %}{% set fit = roles|selectattr('fit')|sum(attribute='n') %}
<div class="zm-roles" style="margin-top:8px"><span class="muted">Open roles ({{ total }}{% if fit and fit < total %}, {{ fit }} on your desk{% endif %}):</span>
{% for j in roles[:80] %}{% if loop.index == show + 1 %}<details><summary class="muted" style="cursor:pointer;font-size:13px;padding:4px 0">{{ roles|length - show }} more role{{ 's' if roles|length - show != 1 }}</summary>{% endif %}
<div style="margin-left:8px;font-size:14px">• {% if j.url %}<a href="{{ j.url }}" target="_blank" rel="noopener">{% endif %}{% if j.fit %}<b>{{ j.title }}</b>{% else %}{{ j.title }}{% endif %}{% if j.url %}</a>{% endif %}{% if j.n > 1 %} ×{{ j.n }}{% endif %}
{%- if j.location %} <span class="muted">· {{ j.location }}</span>{% endif %}{% if j.age %} <span class="muted">· {{ j.age }}</span>{% endif %}</div>
{% if loop.last and loop.length > show %}</details>{% endif %}{% endfor %}</div>{% endif %}{% endmacro %}"""
LOXO_MACRO = """{% macro loxobtn(p) %}{% if p and loxo_on %}<form class="fb" method="post" target="_blank" action="{{ url_for('person_loxo', pid=p.id) }}" style="display:inline">
<button class="ghost" style="background:transparent;color:var(--accent);border:1px solid var(--line);padding:3px 10px;font-size:13px" title="Adds them to Loxo as a Contact and opens their LinkedIn profile: click the Loxo extension there to find their email and phone. Zoomollo copies in whatever Loxo finds.">{{ 'In Loxo · find contact info' if p.loxo_person_id else 'Add to Loxo & find contact info' }}</button></form>
{% if p.loxo_person_id %}<form class="fb" method="post" action="{{ url_for('person_loxo_channels', pid=p.id) }}" style="display:inline">
<button class="ghost" style="background:transparent;color:var(--accent);border:1px solid var(--line);padding:3px 10px;font-size:13px" title="Adds emails and phones Zoomollo found that are not already on this Loxo record. Does not change or remove anything Loxo already has.">Push new contact info</button></form>{% endif %}{% endif %}{% endmacro %}"""
CHANNEL_MACRO = """{% macro chanlist(chs, skip=[]) %}{% set chs = chs|rejectattr('value', 'in', skip)|list %}{% if chs %}<div class="zm-chans" style="margin-top:4px;font-size:13px">{% for ch in chs %}{% if ch.type == 'phone' %}📞 <a href="tel:{{ ch.value }}">{{ ch.value|phone }}</a>{% else %}✉ <a href="mailto:{{ ch.value }}">{{ ch.value }}</a>{% endif %}{% if ch.label %} <span class="muted">{{ ch.label|lower }}</span>{% endif %}{{ ' · ' if not loop.last }}{% endfor %}</div>{% endif %}{% endmacro %}
{% macro otherlist(others, company) %}{% if others %}<details class="zm-others" style="margin-top:6px"><summary class="muted" style="cursor:pointer;font-size:13px;padding:4px 0">{{ others|length }} other contact{{ 's' if others|length != 1 }} at {{ company }}</summary>
{% for o in others %}<div style="margin:6px 0 0 8px;font-size:14px"><b>{{ o.name }}</b> · {{ o.title or '' }} · <a href="{{ o.linkedin_url or li_search(o.name, company) }}" target="_blank" rel="noopener" style="font-size:13px">LinkedIn</a>
{{ chanlist(o.channels) }}{% if not o.channels %}<div class="muted" style="font-size:13px">no email or phone yet</div>{% endif %}
{% if not o.email or o.email_status == 'guessed' or not o.phone %}<div style="margin-top:2px">{{ loxobtn(o) }}</div>{% endif %}</div>{% endfor %}</details>{% endif %}{% endmacro %}"""
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
nav a .n,details.more .menu a .n{font-size:12px;color:var(--muted);background:var(--seg);border-radius:10px;padding:1px 6px;font-weight:400}
.count{color:var(--muted);font-size:13px;margin:-6px 0 10px}
/* More menu: less-used pages, display switches, share */
details.more{position:relative;margin-left:auto}details.more summary{list-style:none;cursor:pointer;color:var(--muted)}
details.more summary::-webkit-details-marker{display:none}details.more summary.on{color:var(--ink);font-weight:600}
details.more .menu{position:absolute;right:0;top:30px;z-index:30;background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px;min-width:270px;display:flex;flex-direction:column;gap:8px;box-shadow:0 6px 24px rgba(0,0,0,.12)}
details.more .menu a{text-decoration:none;color:var(--ink)}details.more .menu a.on{font-weight:600}
details.more hr{border:none;border-top:1px solid var(--line);margin:4px 0;width:100%}
.toggles{display:flex;flex-direction:column;gap:8px}
a.share{text-decoration:none;color:var(--ink);background:var(--seg);border:1px solid var(--line);border-radius:10px;padding:7px 12px;font-size:13px;white-space:nowrap;text-align:center}
a.share:hover{border-color:var(--accent)}
html.mobile details.more{margin-left:0}html.mobile details.more .menu{position:static;box-shadow:none;margin-top:8px}
.seg{display:inline-flex;align-items:center;background:var(--seg);border:1px solid var(--line);border-radius:10px;padding:3px;gap:2px}
.seg-l{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);padding:0 6px 0 6px}
.seg button{background:transparent;color:var(--ink);border:none;border-radius:7px;padding:6px 8px;font-size:13px;white-space:nowrap}
.seg button[aria-pressed="true"]{background:var(--accent);color:#fff;font-weight:600}
.seg button:focus-visible,a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,
textarea:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
a.skip{position:absolute;left:-9999px;top:8px;z-index:50;background:var(--card);color:var(--ink);padding:8px 12px;border-radius:8px;border:1px solid var(--accent)}
a.skip:focus{left:8px}
@media (max-width:1440px){html:not(.mobile) .seg-l{display:none}html:not(.mobile) nav{gap:9px;font-size:14px}}
/* Mobile layout: stacked cards, bigger tap targets */
html.mobile header{flex-direction:column;flex-wrap:nowrap;align-items:stretch;gap:10px;padding:12px}
html.mobile .brand{display:flex;justify-content:space-between;align-items:center}
html.mobile nav{flex-wrap:nowrap;overflow-x:auto;gap:4px;margin:0 -12px;padding:0 12px;scrollbar-width:none}
html.mobile nav a{padding:6px 10px;border-radius:20px;background:var(--seg);white-space:nowrap;font-size:15px;scroll-snap-align:start}
html.mobile nav{scroll-snap-type:x proximity;-webkit-mask-image:linear-gradient(to right,#000 calc(100% - 28px),transparent);mask-image:linear-gradient(to right,#000 calc(100% - 28px),transparent)}
html.mobile nav a .n{font-size:11px;padding:0 5px}
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
td.em{white-space:normal;min-width:230px;max-width:300px}mark{border-radius:3px;padding:0 2px;color:#111}
.k0{background:#fde68a}.k1{background:#bbf7d0}.k2{background:#bfdbfe}.k3{background:#fbcfe8}
.k4{background:#fed7aa}.k5{background:#ddd6fe}.k6{background:#99f6e4}.k7{background:#d9f99d}
.kchip{display:inline-block;border-radius:20px;padding:3px 10px;font-size:13px;margin:2px 4px 2px 0;color:#111;font-weight:600;border:0;cursor:pointer}
.kchip.miss{background:transparent!important;color:var(--muted);border:1px dashed var(--line);font-weight:400;cursor:default}
.kchip .n{font-weight:700;margin-left:4px;opacity:.75}.resume{white-space:pre-wrap;font-size:14px;line-height:1.55;background:var(--card);
border:1px solid var(--line);border-radius:10px;padding:16px;max-height:75vh;overflow:auto}.cand{padding:12px 14px}
.snip{font-size:13px;color:var(--muted);margin-top:4px}.snip mark{font-size:13px}.fitlbl{font-size:12px;font-weight:600;color:var(--ok-ink)}
.score{display:inline-block;min-width:38px;text-align:center;font-weight:700;border-radius:8px;padding:3px 6px;background:var(--chip)}
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
.callbtn{display:none}html.mobile a.callbtn{display:block;text-align:center;margin-top:10px;padding:13px;font-size:17px;font-weight:600;background:var(--accent);color:#fff;border-color:var(--accent)}
html.mobile .zm-log button{padding:12px 14px;flex:1 1 40%}html.mobile .zm-log input[name=note]{flex:1 1 100%}
#zm-toast{position:fixed;left:12px;right:12px;bottom:14px;z-index:60;display:flex;gap:10px;align-items:center;justify-content:space-between;max-width:560px;margin:0 auto;padding:12px 14px;border-radius:10px;background:var(--ink);color:var(--bg);box-shadow:0 6px 24px rgba(0,0,0,.35)}
#zm-toast[hidden]{display:none}#zm-toast button{background:transparent;color:var(--bg);border:1px solid currentColor}
/* Phone: one slim row. Tabs scroll sideways, More sits at the right, the name only shows while something is running. */
html.mobile header{flex-direction:row;flex-wrap:wrap;align-items:center;gap:6px;padding:6px 10px;position:relative}
html.mobile .brand{display:none}html.mobile .brand:has(.chip){display:flex;width:100%;justify-content:flex-end}
html.mobile .brand b{display:none}
html.mobile nav{flex:1 1 0;min-width:0;margin:0;padding:0}
html.mobile details.more{flex:none;position:static}
html.mobile details.more > summary{padding:0 12px;border-radius:20px;background:var(--seg);white-space:nowrap}
html.mobile details.more .menu{position:absolute;left:8px;right:8px;top:100%;margin-top:4px;box-shadow:0 8px 24px rgba(0,0,0,.35)}
html.mobile main h2{margin-top:0}
.zm-sum{font-size:13.5px;color:var(--muted);margin-top:2px;line-height:1.35}
.zm-phone{display:none}html.mobile button.zm-phone{display:block}
.zm-linekind{display:none}html.mobile .zm-linekind{display:block;text-align:center;font-size:13px;margin-top:4px}
html.mobile .zm-card a:not(.callbtn),html.mobile .zm-run a:not(.zm-dialbtn){display:inline-block;padding:12px 0;min-height:44px;box-sizing:border-box}
html.mobile .zm-card summary,html.mobile .zm-run summary{min-height:44px;display:flex;align-items:center;gap:6px;padding:0 4px}
html.mobile .zm-card button,html.mobile .zm-run button,html.mobile #zm-toast button,html.mobile main .preview button{min-height:44px}
html.mobile nav a,html.mobile details.more > summary,html.mobile details.more .menu a{min-height:44px;display:flex;align-items:center}
html.mobile details.more .menu a{padding:0 4px}
html.mobile .zm-log input[name=note],html.mobile #zm-log input[name=note]{min-height:44px}
html.mobile main .sub a,html.mobile main > div > span > a{display:inline-block;padding:12px 0}
html.mobile main .pcard a,html.mobile main td a,html.mobile main .person a,html.mobile main .cand a,html.mobile main .tl a{display:inline-block;padding:12px 0;min-height:44px;box-sizing:border-box}
html.mobile main summary{min-height:44px;display:flex;align-items:center;gap:6px}
html.mobile main input:not([type=checkbox]):not([type=radio]):not([type=hidden]),html.mobile main select{min-height:44px}
html.mobile a.btn,html.mobile button{min-height:44px}html.mobile a.btn{display:inline-flex;align-items:center;justify-content:center}
html.mobile a.btn.callbtn,html.mobile a.btn.zm-dialbtn{display:flex}
select{max-width:100%}
html.mobile .grid > *,html.mobile .row > *{min-width:0;max-width:100%}
html.mobile .row select,html.mobile .row input:not([type=checkbox]):not([type=radio]){flex:1 1 100%}
html.mobile .chip{white-space:normal}html.mobile .card,html.mobile td{overflow-wrap:anywhere}
html.mobile .zm-card .zm-score{display:none}html.mobile .zm-card .zm-score-in{display:inline}
.zm-score-in{display:none}
html.mobile .zm-card .zm-links a{display:inline-block;padding:9px 0}html.mobile .zm-card .zm-tel{display:none}
details.zm-fold > summary{cursor:pointer;font-weight:600;padding:10px 0;list-style-position:inside}
html.mobile details.zm-fold > summary{padding:14px 12px;border:1px solid var(--line);border-radius:10px;background:var(--card);margin-bottom:12px}
html:not(.mobile) details.zm-fold > summary{display:none}
html.mobile .zm-sticky{position:sticky;bottom:0;z-index:20;background:var(--card);margin:0 -20px -20px;padding:10px 12px calc(10px + env(safe-area-inset-bottom));border-top:1px solid var(--line);box-shadow:0 -6px 18px rgba(0,0,0,.25)}
#zm-load{position:fixed;top:0;left:0;height:3px;width:0;background:var(--accent);z-index:99;opacity:0}
#zm-load.on{width:88%;opacity:1;transition:width 5s cubic-bezier(.1,.7,.2,1)}
</style></head><body><div id="zm-load" aria-hidden="true"></div>
{% set more_tabs = [('changes','This week'),('opened','Opened my email'),('contacts','Contacts'),('messages','Messages & templates')] %}
<a class="skip" href="#main">Skip to content</a>
<header><div class="brand"><b>Zoomollo</b>{% if busy %} <span class="chip" title="Searches and crawls in the queue">{{ busy }} running</span>{% endif %}</div><nav>
{% for t,label in [('today_page','Today'),('scoreboard_page','Scoreboard'),('companies','Accounts'),('pipeline_page','Pipeline'),('people','People'),('jobs','Jobs'),('candidates','Candidates'),('find','Find companies')] %}
<a href="{{ url_for(t) }}" class="{{ 'on' if tab==t }}">{{ label }}{% if counts.get(t) is not none %} <span class="n">{{ '{:,}'.format(counts[t]) }}</span>{% endif %}</a>{% endfor %}</nav>
<details class="more" id="zm-more"><summary class="{{ 'on' if tab in more_tabs|map('first')|list }}">More ▾</summary><div class="menu">
{% for t,label in more_tabs %}<a href="{{ url_for(t) }}" class="{{ 'on' if tab==t }}">{{ label }}{% if counts.get(t) is not none %} <span class="n">{{ '{:,}'.format(counts[t]) }}</span>{% endif %}</a>{% endfor %}
<hr>
{% set share_body %}Hi,

Here's Zoomollo, the contact-discovery tool I've been using. It runs on your own Windows PC and starts empty.

1. Download: https://github.com/cjrglad12/Zoomollo/archive/refs/heads/main.zip
   (The project is private. If the link says Not Found, ask for GitHub access first.)
2. Unzip it to Documents\\Zoomollo so start.bat is directly inside that folder.
3. If you don't have Python 3.10 or newer, install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
4. Double-click start.bat. The first run sets everything up (a minute or two), then Zoomollo opens in your browser.
5. To connect Loxo: open zoomollo-settings.txt in that folder, fill in LOXO_AGENCY_SLUG and your own LOXO_API_KEY (Loxo > Settings > API Keys), save, and run start.bat again.
{% endset %}
<div class="muted" style="font-size:12px;padding:6px 4px">Version: started {{ version }}</div>
<a class="share" href="mailto:?subject={{ 'Zoomollo: download and setup'|urlencode }}&amp;body={{ share_body|urlencode }}" title="Email someone a download link and setup steps">✉ Share</a>
<div class="toggles">
<div class="seg" role="group" aria-label="Theme"><span class="seg-l">Theme</span>
<button type="button" data-set="theme" data-val="light" onclick="zmSet('theme','light')">☀︎ Light</button>
<button type="button" data-set="theme" data-val="auto" onclick="zmSet('theme','auto')">Auto</button>
<button type="button" data-set="theme" data-val="dark" onclick="zmSet('theme','dark')">☾ Dark</button></div>
<div class="seg" role="group" aria-label="Layout"><span class="seg-l">Layout</span>
<button type="button" data-set="layout" data-val="desktop" onclick="zmSet('layout','desktop')">🖥 PC</button>
<button type="button" data-set="layout" data-val="auto" onclick="zmSet('layout','auto')">Auto</button>
<button type="button" data-set="layout" data-val="mobile" onclick="zmSet('layout','mobile')">📱 Mobile</button></div>
</div></div></details></header>
<main id="main" tabindex="-1">
{% for n in notices %}<div class="preview" role="status" style="margin-bottom:10px;border-color:var(--ok-ink)">✓ {{ n.text }}{% if n.tab %} <a href="{{ url_for(n.tab) }}">View</a>{% endif %}</div>{% endfor %}
{% if setup %}<div class="warn" style="margin-bottom:14px"><b>Finish setting up:</b>
{% for label, link in setup %}<a href="{{ link }}">{{ label }}</a>{{ ' · ' if not loop.last }}{% endfor %}</div>{% endif %}
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
// Phone: keep the tab you're on in view in the sideways-scrolling tab row.
(function () {
  var on = document.querySelector("nav a.on"), nav = document.querySelector("nav");
  if (on && nav && document.documentElement.classList.contains("mobile")) {
    var r = on.getBoundingClientRect(), n = nav.getBoundingClientRect();
    if (r.left < n.left || r.right > n.right - 24) nav.scrollLeft += r.left - n.left - 8;
  }
})();
// Screen readers: every field gets a name (from its placeholder if it has no label),
// and decorative icons are skipped.
document.querySelectorAll("input:not([type=hidden]), textarea, select").forEach(function (el) {
  if (!el.getAttribute("aria-label") && !el.closest("label") && el.placeholder) el.setAttribute("aria-label", el.placeholder);
});
document.querySelectorAll("main *").forEach(function (el) {
  el.childNodes.forEach(function (n) {
    if (n.nodeType === 3 && /[📞✉👤]/u.test(n.nodeValue) && n.nodeValue.trim().length <= 3) {
      var s = document.createElement("span"); s.setAttribute("aria-hidden", "true"); s.textContent = n.nodeValue;
      n.replaceWith(s);
    }
  });
});
// Every tap shows it was received: a bar across the top while the next page loads,
// and a form can't be sent twice by a double tap.
(function () {
  var bar = document.getElementById("zm-load"), t;
  function busy() { bar.classList.add("on"); clearTimeout(t); t = setTimeout(idle, 15000); }
  function idle() { bar.classList.remove("on"); document.querySelectorAll("form[data-busy]").forEach(function (f) { delete f.dataset.busy; }); }
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a[href]");
    if (!a || a.target || e.defaultPrevented || e.ctrlKey || e.metaKey || e.shiftKey || a.origin !== location.origin) return;
    if (/[.]csv$/.test(a.pathname) || (a.pathname === location.pathname && a.hash)) return;
    busy();
  });
  document.addEventListener("submit", function (e) {
    if (e.defaultPrevented) return;
    var f = e.target;
    if (f.dataset.busy) { e.preventDefault(); return; }
    if (!f.target && !/[.]csv$/.test(f.action)) { f.dataset.busy = "1"; busy(); }
  });
  window.addEventListener("pageshow", function (e) { idle(); if (e.persisted) location.reload(); });
})();
// Long sections start folded on a phone; on a PC they stay open.
(function () {
  var mobile = document.documentElement.classList.contains("mobile");
  document.querySelectorAll("details.zm-fold").forEach(function (d) {
    var target = location.hash.length > 1 && d.querySelector(location.hash);
    d.open = !mobile || !!target || d.hasAttribute("data-keep");
    if (target) target.scrollIntoView();
  });
})();
// Close the More menu when clicking anywhere else.
document.addEventListener("click", function (e) {
  var m = document.getElementById("zm-more");
  if (m && m.open && !m.contains(e.target)) m.open = false;
});
</script></body></html>"""

FIND = """<div class="grid">
<form class="card big" method="post" action="{{ url_for('enqueue') }}"><h3>Find companies</h3>
<p>Describe the companies and the city. Zoomollo finds them, crawls their sites and job boards, maps their people and flags likely hiring managers.</p>
<input type="hidden" name="action" value="prospect">
<div class="row"><input name="what" placeholder="What they do: CPA firms, MEP engineering, cybersecurity…" required>
<input name="where" placeholder="City: Dallas, TX" required>
<input name="limit" type="number" value="25" min="1" max="500" style="max-width:110px" title="Max companies">
<button>Find companies</button></div>
<p class="muted" style="margin-top:8px">Sources: map data, chamber and association member lists, city and state license / permit records, and links between company websites. No search engines or paid services.</p></form>
<form class="card" method="post" action="{{ url_for('enqueue') }}"><h3>Add a company you know</h3>
<p>Its website, what it does and where. Zoomollo saves it under that vertical and city, then finds its people and open roles.</p>
<input type="hidden" name="action" value="addco">
<label class="muted" style="font-size:12px">Website<input name="site" placeholder="ziosk.com" required style="width:100%;margin:2px 0 8px"></label>
<div class="row"><label class="muted" style="font-size:12px;flex:1">What they do<input name="what" placeholder="artificial intelligence" style="width:100%;margin-top:2px"></label>
<label class="muted" style="font-size:12px;flex:1">City<input name="where" placeholder="Dallas, TX" style="width:100%;margin-top:2px"></label></div>
<button style="margin-top:8px">Add company</button></form>
<form class="card" method="post" action="{{ url_for('enqueue') }}"><h3>Directory sites</h3>
<p>A page that lists companies: an association's members, an event's sponsors, an incubator's portfolio. One link per line. Every company it links to is checked, saved and crawled.</p>
<input type="hidden" name="action" value="directory">
<textarea name="items" placeholder="https://www.techtitans.org/members"></textarea>
<div class="row" style="margin-top:8px"><input name="what" placeholder="Must mention (optional): cybersecurity" style="flex:1;min-width:0">
<input name="where" placeholder="City: Dallas, TX" required style="flex:1;min-width:0">
<input name="limit" type="number" value="40" min="1" max="300" style="max-width:80px" title="Max companies"></div>
<button style="margin-top:8px">Read directory</button></form>
<div class="card"><h3>Activity</h3><div class="log">{{ log|join('\\n') or 'Nothing running.' }}</div>
<p style="margin-top:10px"><a href="{{ url_for('find') }}">Refresh</a>{% if hidden_log %} · <a href="{{ url_for('find', all=1) }}">show all {{ hidden_log }} routine lines</a>{% endif %}</p></div>
</div>
<details class="card" style="margin-top:16px" {{ 'open' if guesses or bounced }}><summary><b>More tools</b> <span class="muted">· crawl specific sites, import job boards, public data sources, bounced emails, guess an email</span></summary>
<div class="grid" style="margin-top:14px">
<form class="card" method="post" action="{{ url_for('enqueue') }}"><h3>Crawl specific sites</h3>
<p>One domain per line.</p><input type="hidden" name="action" value="crawl">
<textarea name="items" placeholder="acme.com"></textarea><button>Crawl</button></form>
<form class="card" method="post" action="{{ url_for('enqueue') }}"><h3>Import job boards</h3>
<p>board:slug per line (greenhouse, lever, ashby, workable), a bare slug to try all, or a Workday careers link (…myworkdayjobs.com/…).</p>
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
</div></details>"""

PEOPLE = EMAIL_MACRO + LINKEDIN_MACRO + OL_MACRO + PHONE_MACRO + """<form class="bar"><input type="search" name="q" value="{{ q }}" placeholder="Name, title, company, city, industry…">
<select name="department"><option value="">All departments</option>{% for k,v in depts.items() %}<option value="{{ k }}" {{ 'selected' if k==dept }}>{{ v }}</option>{% endfor %}</select>
<label class="chip" style="padding:8px 10px"><input type="checkbox" name="hiring" value="1" {{ 'checked' if hiring }} style="padding:0"> Hiring managers</label>
<button>Search</button><a class="btn" href="{{ url_for('export_people', q=q, department=dept, hiring=hiring) }}">Export CSV</a></form>
{% if rows and sequences and (q or dept or hiring) %}<form class="bar" method="post" action="{{ url_for('enroll_bulk') }}"
 onsubmit="return confirm('Start follow-ups for {{ [rows|length, bulk_cap]|min }} {{ 'person' if rows|length == 1 else 'people' }}{% if rows|length > bulk_cap %} (the first {{ bulk_cap }} of {{ rows|length }} shown){% endif %}? Off-limits companies and people with no email or phone are skipped.')">
<input type="hidden" name="q" value="{{ q }}"><input type="hidden" name="department" value="{{ dept or '' }}"><input type="hidden" name="hiring" value="{{ '1' if hiring else '' }}">
<span class="muted" style="align-self:center">Start follow-ups for the {{ [rows|length, bulk_cap]|min }} shown{% if rows|length > bulk_cap %} (first {{ bulk_cap }}){% endif %}:</span>
<select name="sequence">{% for sq in sequences %}<option value="{{ sq.id }}">{{ sq.name }}</option>{% endfor %}</select>
<button class="ghost" style="background:transparent;color:var(--accent);border:1px solid var(--line)" title="Skips off-limits companies and people without an email or phone">Start follow-ups</button></form>
{% elif rows and sequences %}<p class="muted" style="font-size:13px;margin:-4px 0 10px">To start follow-ups for a group, search or filter first (for example tick Hiring managers).</p>{% endif %}
<div class="count">{{ count_line }}</div>
<div class="wrap"><table><tr><th>Name</th><th>Title</th><th>Company</th><th>Email</th><th>Phone</th><th>Hiring for</th><th></th></tr>
{% for p in rows %}<tr><td><b>{{ p.name }}</b></td><td>{{ p.title or '' }}</td>
<td><a href="{{ url_for('company', cid=p.company_id) }}">{{ p.company_name or p.company_key }}</a> {{ olbadge(ol(p.company_id)) }}</td>
<td class="v em">{{ email_cell(p) }}</td>
<td>{{ phonecell(p.phone, cophone(p.company_id)) }}{% if p.phone %} <a class="msg" href="{{ url_for('message', person=p.id) }}" title="Send a text">💬</a>{% endif %}</td><td class="wraptext">{% if p.hiring_for %}<span class="chip hm">{{ p.hiring_for }}</span>{% endif %}</td>
<td>{{ linkedin(p) }}{% if (p.email or p.phone) and sequences and not ol(p.company_id).blocked %}
<form class="fb" method="post" action="{{ url_for('enroll') }}"><input type="hidden" name="person" value="{{ p.id }}">
<input type="hidden" name="sequence" value="{{ sequences[0].id }}"><button class="ghost" style="background:transparent;color:var(--accent);border:1px solid var(--line);margin-top:4px" title="Start: {{ sequences[0].name }}">Follow up</button></form>{% endif %}</td></tr>
{% else %}<tr><td colspan="7">No people yet. Start on the <a href="{{ url_for('find') }}">Prospect</a> tab.</td></tr>{% endfor %}</table></div>"""

COMPANY = EMAIL_MACRO + LINKEDIN_MACRO + OL_MACRO + SCORE_MACRO + PHONE_MACRO + """<h2>{{ co.name or co.key }}</h2>
<div class="sub">{% if co.domain %}<a href="https://{{ co.domain }}" target="_blank" rel="noopener">{{ co.domain }}</a> · {% endif %}
{% if co.industry %}{{ co.industry }} · {% endif %}{% if co.city %}{{ co.city }} · {% endif %}
{% if co.phone %}<span class="v">{{ co.phone|phone }}</span> <a class="msg" href="{{ url_for('message', company=co.id) }}" title="Send a text">💬</a> · {% elif cophone(co.id) %}<span class="v">{{ cophone(co.id)|phone }}</span> · {% else %}<span class="muted">phone not found</span> · {% endif %}{% if co.address %}{{ co.address }} · {% endif %}
{% if co.email_pattern %}email format <span class="v">{{ co.email_pattern }}@{{ co.domain }}</span> ({{ ((co.pattern_confidence or 0)*100)|int }}%){% endif %}
<form method="post" action="{{ url_for('company_headcount', cid=co.id) }}" style="display:inline"> · <label>Headcount
<input name="headcount" value="{{ co.headcount or '' }}" placeholder="unknown" inputmode="numeric" style="width:76px;padding:2px 6px"></label>
{% if co.headcount %}<span class="muted">({{ {'website': 'from their site', 'apollo': 'from Apollo', 'apollo-range': 'Apollo says 101–500; exact number not known', 'you': 'set by you'}.get(co.headcount_source, co.headcount_source or '') }})</span>{% endif %}
<button class="ghost" style="padding:2px 8px">Save</button></form></div>
<form method="post" action="{{ url_for('company_summary', cid=co.id) }}" class="row" style="margin:-4px 0 12px;align-items:center">
<input name="summary" value="{{ co.summary or '' }}" maxlength="140" placeholder="What they do, in one line (shown on call cards)" aria-label="Company summary" style="flex:1;min-width:200px">
<button class="ghost">Save summary</button>{% if co.summary and co.summary_source == 'website' %}<span class="muted" style="font-size:12px">from their website; edit to improve</span>{% endif %}</form>
{% set conum = co.phone or cophone(co.id) %}{% if conum %}<a class="btn callbtn" href="tel:{{ conum }}" style="margin:0 0 12px">📞 Call {{ conum|phone }}{% if conum|linenote %} ({{ conum|linenote }}){% endif %}</a>{% endif %}
{% if not co.domain %}<div class="warn" style="margin-bottom:12px"><b>No website yet</b>, so no people, roles or emails can be found for this company.
Look it up, paste its website here, and Zoomollo reads it in the background.
<form method="post" action="{{ url_for('company_website', cid=co.id) }}" class="row" style="margin-top:8px">
<input name="website" placeholder="e.g. walkerengineering.com" required style="flex:1;min-width:200px">
<button>Add website</button>
<a class="btn" href="https://www.google.com/search?q={{ ((co.name or '') ~ ' ' ~ (co.city or ''))|urlencode }}" target="_blank" rel="noopener">Look it up</a>
</form></div>{% elif request.args.get('added') %}<div class="preview" style="margin-bottom:12px">Reading {{ co.domain }} now. People, roles and emails appear here in a minute or two; refresh to see them.</div>{% endif %}
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

{% if leads %}<div class="section"><b>Leaders found in Apollo</b> <span class="muted">· {{ leads|length }} · first names and titles; last names hidden until looked up</span></div>
<div class="card" style="margin-bottom:12px">{% for l in leads %}<div style="padding:3px 0"><b>{{ l.first_name }} {{ l.last_name or '' }}</b>{% if not l.last_name %} {{ l.last_masked or '' }}{% endif %} <span class="muted">· {{ l.title or '' }}</span>
{% if l.email %} · <a href="mailto:{{ l.email }}">{{ l.email }}</a> <span class="chip">{{ 'verified' if l.email_status == 'verified' else (l.email_status or 'unverified') }}</span>{% endif %}
{% set left = apollo_left() %}{% if left is not none and not l.enriched_at %}<form class="fb" method="post" action="{{ url_for('lead_enrich', lid=l.id) }}" style="display:inline">
<button class="ghost" {{ 'disabled' if not left }} title="Uses one free Apollo credit; personal emails and phones are never requested">Get full name &amp; email</button></form>
<span class="muted" style="font-size:12px">{{ left }} free Apollo credit{{ '' if left == 1 else 's' }} left this month</span>{% elif l.enriched_at and not l.last_name %}<span class="muted" style="font-size:12px">· Apollo had no more on them</span>{% endif %}</div>{% endfor %}</div>{% endif %}
<div class="section"><b>Org map</b> <span class="muted">· {{ people|length }} people</span></div>
<details class="zm-fold" open><summary>Show all {{ people|length }} people</summary>
{% if people %}<div class="org">{% for d, ps in org %}<div class="card dept"><h4>{{ depts[d] }}</h4>
{% for p in ps %}<div class="person"><div class="n">{{ p.name }} {% if p.hiring_for %}<span class="chip hm">hiring</span>{% endif %}</div>
<div class="t">{{ p.title or '' }}{% if p.seniority %} · {{ levels[p.seniority] }}{% endif %}</div>
<div class="c v">{{ email_cell(p) }}
{% if p.phone %}<br>{{ p.phone|phone }} <a class="msg" href="{{ url_for('message', person=p.id) }}" title="Send a text">💬</a>{% else %}<br><span class="muted">no direct phone</span>{% endif %}
{% if loxo_on and p.loxo_person_id %}<form class="fb" method="post" action="{{ url_for('person_loxo_channels', pid=p.id) }}" style="display:inline">
<button class="ghost" title="Adds emails and phones Zoomollo found that are not already on this Loxo record. Does not change or remove anything Loxo already has.">Push new contact info to Loxo</button></form>{% endif %}
<br>{{ linkedin(dict(p, company_name=co.name or co.key)) }}</div>
</div>{% endfor %}</div>{% endfor %}</div>
{% else %}<p class="muted">No people found yet.</p>{% endif %}</details>

<div class="section"><b>Open roles × contacts</b> <span class="muted">· <a href="{{ url_for('candidates', q=(jobs|map(attribute='title')|first or '')) }}">find candidates</a></span> <span class="muted">· {{ jobs|length }}</span></div>
<div class="wrap"><table><tr><th>Role</th><th>Location</th><th>Source</th><th>Matched contacts</th><th>Candidates to pitch</th></tr>
{% for j in jobs %}<tr><td>{% if j.url %}<a href="{{ j.url }}" target="_blank" rel="noopener">{{ j.title }}</a>{% else %}{{ j.title }}{% endif %}</td>
<td>{% if j.location %}{{ j.location }}{% else %}<span class="muted" title="The post doesn't say where; counted as local only because of the company's address">location unknown</span>{% endif %}</td><td><span class="chip">{{ j.board }}</span></td><td class="wraptext">{% for m in matches.get(j.id, []) %}
<div><span class="reason">{{ m.reason }}</span> {% if m.name %}{{ m.name }} <span class="muted">({{ m.title or '' }})</span>{% endif %}
<span class="v">{{ m.email or m.value or '' }}</span></div>{% else %}<span class="muted">none yet</span>{% endfor %}</td>
<td class="wraptext">{% set ps = pitch_for(j) %}{% for m in ps %}<div><a href="{{ url_for('candidate', cid=m.c.id) }}">{{ m.c.name }}</a> <span class="muted">{{ m.c.title }}{% if m.c.city %}, {{ m.c.city }}{% endif %}</span> · <a href="{{ url_for('pitch_page', candidate=m.c.id, job=j.id) }}">pitch</a></div>
{% else %}<span class="muted">{{ 'none in Loxo' if desk_fit(j) else '' }}</span>{% endfor %}</td></tr>
{% else %}<tr><td colspan="5">No open roles found.</td></tr>{% endfor %}</table></div>

<div class="section"><b>Company contacts</b></div>
<div class="wrap"><table><tr><th>Contact</th><th>Kind</th><th>Source</th></tr>
{% for r in contacts %}<tr><td class="v">{{ r.value }}</td><td><span class="chip">{{ r.kind }}</span></td>
<td>{% if r.source_url and r.source_url.startswith('http') %}<a href="{{ r.source_url }}" target="_blank" rel="noopener">page</a>{% else %}{{ r.source_url or '' }}{% endif %}</td></tr>
{% else %}<tr><td colspan="3">None.</td></tr>{% endfor %}</table></div>
<details class="card" style="margin-top:24px"><summary class="muted">Not a prospect? (staffing / recruiting firm)</summary>
<p class="muted" style="margin:8px 0">Blocking deletes everything saved for this company and keeps it out of future searches.</p>
<form class="fb" method="post" action="{{ url_for('block', cid=co.id) }}"
 onsubmit="return confirm('Block {{ (co.name or co.key)|e }} as a staffing/recruiting firm? Everything saved for it is deleted.')">
<button class="no">Block this company</button></form></details>"""

SCOREBOARD = """<h2>Scoreboard</h2>
<div class="sub">This week · {{ w.start.strftime('%a %b ') ~ w.start.day }} – {{ w.end.strftime('%a %b ') ~ w.end.day }}
{% if w.pace.expected %} · {% if w.pace.actual >= w.pace.expected %}<b style="color:var(--ok-ink)">on pace</b>{% else %}<b style="color:var(--bad-ink)">{{ w.pace.expected - w.pace.actual }} dials behind pace</b>{% endif %}
({{ w.pace.actual }} so far, {{ w.pace.expected }} by the end of today){% endif %}</div>
{% if not w.this.dials and not w.this.emails and not w.this.pitches and not w.this.texts %}
<div class="card" style="margin-bottom:14px"><p style="margin:0">Nothing logged yet this week. Every outcome you log on <a href="{{ url_for('today_page') }}">Today</a>, every pitch you send and every follow-up email counts here.</p></div>{% endif %}
<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr))">
{% for label, n, goal, hint in w.progress %}{% set pct = [100, (100 * n / goal)|round|int]|min if goal else 0 %}
<div class="card"><div class="muted" style="font-size:13px">{{ label }}</div>
<div style="font-size:28px;font-weight:700">{{ n }} <span class="muted" style="font-size:15px;font-weight:400">/ {{ goal }}</span></div>
<div role="progressbar" aria-label="{{ label }}" aria-valuenow="{{ n }}" aria-valuemin="0" aria-valuemax="{{ goal }}" style="height:8px;background:var(--seg);border-radius:6px;overflow:hidden;margin:6px 0">
<div style="height:100%;width:{{ pct }}%;background:{{ 'var(--ok-ink)' if pct >= 100 else 'var(--accent)' }}"></div></div>
<div class="muted" style="font-size:12px">{{ hint }} · last week {{ {'Dials': w.last.dials, 'Conversations': w.last.connects, 'Candidate pitches': w.last.pitches, 'Meetings booked': w.last.meetings}[label] }}</div></div>{% endfor %}</div>
<div class="card" style="margin-top:14px"><h3>Conversion</h3>
<p style="font-size:15px;color:var(--ink)">{{ w.this.dials }} dial{{ 's' if w.this.dials != 1 }} → {{ w.this.connects }} conversation{{ 's' if w.this.connects != 1 }} <span class="muted">({{ w.rates.connect }}; last week {{ w.rates.connect_last }})</span>
→ {{ w.this.meetings }} meeting{{ 's' if w.this.meetings != 1 }} <span class="muted">({{ w.rates.meeting }} of conversations; last week {{ w.rates.meeting_last }})</span></p>
<p>Agency benchmarks to aim for: about 1 conversation per 8–10 dials, and about 1 meeting per 5–7 conversations.</p></div>
<div class="card" style="margin-top:14px"><h3>Dials by day</h3>
<div style="display:flex;gap:14px;align-items:flex-end;height:150px;padding-top:10px;border-bottom:1px solid var(--line);position:relative">
<div style="position:absolute;left:0;right:0;bottom:{{ (120 * w.goals.daily_dials / w.max_day)|round|int }}px;border-top:1px dashed var(--muted)" aria-hidden="true"></div>
{% for d in w.days %}<div style="flex:1;text-align:center">
<div style="font-size:12px;font-weight:600">{{ d.dials }}</div>
<div title="{{ d.dials }} dials, {{ d.connects }} conversations" style="height:{{ (120 * d.dials / w.max_day)|round|int }}px;min-height:2px;background:{{ 'var(--accent)' if d.dials < w.goals.daily_dials else 'var(--ok-ink)' }};border-radius:4px 4px 0 0;opacity:{{ '1' if d.is_today or d.dials else '.4' }}"></div></div>{% endfor %}</div>
<div style="display:flex;gap:14px">{% for d in w.days %}<div style="flex:1;text-align:center;font-size:12px" class="{{ '' if d.is_today else 'muted' }}">{{ d.label }}{% if d.is_today %} (today){% endif %}<br>{{ d.connects }} conv.</div>{% endfor %}</div>
<p class="muted" style="margin-top:8px">Dashed line: your goal of {{ w.goals.daily_dials }} dials a day.</p></div>
<div class="section"><b>This week vs last week</b></div>
<div class="wrap"><table><tr><th>Activity</th><th>This week</th><th>Last week</th><th>Change</th></tr>
{% for key, label in [('dials','Dials'),('connects','Conversations'),('voicemails','Voicemails left'),('meetings','Meetings booked'),('pitches','Candidate pitches'),('emails','Emails sent'),('texts','Texts sent'),('accounts','Accounts worked')] %}
{% set a, b = w.this[key], w.last[key] %}<tr><td>{{ label }}</td><td><b>{{ a }}</b></td><td>{{ b }}</td>
<td class="{{ 'fit' if a > b else '' }}">{% if a > b %}+{{ a - b }}{% elif a < b %}−{{ b - a }}{% else %}same{% endif %}</td></tr>{% endfor %}</table></div>
<details class="card" style="margin-top:14px"><summary><b>Goals</b> <span class="muted">· {{ w.goals.daily_dials }} dials a day, {{ w.goals.weekly_connects }} conversations, {{ w.goals.weekly_pitches }} pitches, {{ w.goals.weekly_meetings }} meetings a week · Edit</span></summary>
<form method="post" action="{{ url_for('scoreboard_goals') }}" class="row" style="margin-top:10px;align-items:flex-end">
{% for k, label in goal_labels.items() %}<label class="muted" style="font-size:12px;flex:1;min-width:150px">{{ label }}<input name="{{ k }}" type="number" min="1" value="{{ w.goals[k] }}" style="width:100%;margin-top:2px"></label>{% endfor %}
<button>Save goals</button></form></details>
<div class="card" id="loxo" style="margin-top:14px"><h3>Loxo sync</h3>
{% if not lx.enabled %}<p>Off. {% if not loxo_on %}Connect Loxo in zoomollo-settings.txt first.{% else %}Turn it on below to log your calls, emails, texts and pitches in Loxo.{% endif %}</p>
{% else %}<p>Calls, emails, texts and pitches you log here are added to Loxo as activities about {{ delay }} minutes later (so Undo can still take them back).</p>{% endif %}
<p style="font-size:15px;color:var(--ink)"><b>{{ lx.sent }}</b> logged in Loxo · <b>{{ lx.waiting }}</b> waiting · {{ lx.skipped }} skipped · {% if lx.failed %}<b style="color:var(--bad-ink)">{{ lx.failed }} failed</b>{% else %}0 failed{% endif %}</p>
<div class="row" style="align-items:center">
<form method="post" action="{{ url_for('loxo_sync_post') }}" class="fb"><button name="action" value="now">Send now</button>
{% if lx.failed %}<button name="action" value="retry" class="ghost">Retry failed</button>{% endif %}</form>
<form method="post" action="{{ url_for('loxo_sync_post') }}" class="row" style="align-items:center"><input type="hidden" name="action" value="settings">
<label class="muted" style="font-size:13px"><input type="checkbox" name="enabled" value="1" {{ 'checked' if lx.enabled }} style="padding:0" onchange="this.form.submit()"> Log activity in Loxo</label>
<label class="muted" style="font-size:13px"><input type="checkbox" name="create" value="1" {{ 'checked' if lx.create_contacts }} style="padding:0" onchange="this.form.submit()"> Create the contact in Loxo when they're not there yet</label></form></div>
{% if lx.problems %}<div class="wrap" style="margin-top:10px"><table><tr><th>When</th><th>Company</th><th>Contact</th><th>Activity</th><th>Status</th><th>Why</th></tr>
{% for p in lx.problems %}<tr><td>{{ p.happened_at[:10] }}</td><td>{{ p.company_name or '' }}</td><td>{{ p.person_name or '' }}</td><td>{{ p.activity_type }}</td>
<td><span class="chip {{ 'ol-blocked' if p.status == 'failed' else '' }}">{{ p.status }}</span></td><td class="wraptext">{{ p.detail or '' }}</td></tr>{% endfor %}</table></div>{% endif %}
</div>"""

PITCH_SENT = """<h2>Your email app should be open</h2>
<div class="sub">The draft to {{ to or 'the hiring manager' }} is ready there: “{{ subject }}”.
If it didn't open, <a href="{{ mailto }}">open it again</a>.</div>
<div class="card"><h3>Did you send it?</h3><p>Zoomollo only counts it as a touch once you've sent it.</p>
<form method="post" action="{{ url_for('pitch_send') }}" class="row">
<input type="hidden" name="action" value="logsent"><input type="hidden" name="job" value="{{ job }}">
<input type="hidden" name="to" value="{{ to }}"><input type="hidden" name="subject" value="{{ subject }}">
<input type="hidden" name="body" value="{{ body }}"><input type="hidden" name="override" value="{{ override }}">
<input type="hidden" name="candidate" value="{{ candidate }}">
<button>Yes, I sent it</button>
<a class="btn" href="{{ url_for('today_page') }}">No, not yet</a></form></div>
<script>window.location.href = {{ mailto|tojson }};</script>"""

TODAY = PHONE_MACRO + ROLES_MACRO + LOXO_MACRO + CHANNEL_MACRO + """<div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap"><h2>Today</h2>
<a class="btn" href="{{ url_for('blitz_page') }}" style="margin-left:auto" title="Work a call list one card at a time, with keyboard shortcuts">▶ Start call blitz</a></div>
<div class="sub"><span id="zm-left">{{ rows|length }} account{{ 's' if rows|length != 1 }} to call</span> · {{ due|length }} follow-up{{ 's' if due|length != 1 }} due · {{ done|length }} logged today
{% if not desk_set %} · <a href="{{ url_for('companies') }}">set your desk</a> so this list knows which roles you place{% endif %}</div>
{% if prog %}<div style="display:flex;gap:10px;align-items:center;margin:-6px 0 14px;flex-wrap:wrap">
<div role="progressbar" aria-label="Dials today" aria-valuenow="{{ prog.dials }}" aria-valuemin="0" aria-valuemax="{{ prog.goal }}" style="flex:1;min-width:160px;max-width:360px;height:10px;background:var(--seg);border-radius:6px;overflow:hidden">
<div id="zm-bar" style="height:100%;width:{{ prog.pct }}%;background:{{ 'var(--ok-ink)' if prog.pct >= 100 else 'var(--accent)' }}"></div></div>
<span style="font-size:14px"><b id="zm-dials">{{ prog.dials }} of {{ prog.goal }}</b> dials today{% if prog.connects %} · {{ prog.connects }} conversation{{ 's' if prog.connects != 1 }}{% endif %}{% if prog.meetings %} · {{ prog.meetings }} meeting{{ 's' if prog.meetings != 1 }} 🎉{% endif %}
· <a href="{{ url_for('scoreboard_page') }}">Scoreboard</a>
{% if lx and lx.enabled %} · <a href="{{ url_for('scoreboard_page') }}#loxo" class="muted">Loxo: {{ lx.sent }} logged{% if lx.waiting %}, {{ lx.waiting }} waiting{% endif %}{% if lx.failed %}, <b style="color:var(--bad-ink)">{{ lx.failed }} failed</b>{% endif %}</a>{% endif %}</span></div>{% endif %}
{% if last_action %}<div class="preview" role="status" style="margin-bottom:14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
<span>Logged <b>{{ last_action.label }}</b> for {{ last_action.company }}.</span>
<form class="fb" method="post" action="{{ url_for('today_undo') }}"><button class="ghost">Undo</button></form></div>{% endif %}
{% if due %}<div class="card" style="margin-bottom:14px"><h3>Follow-ups due</h3>
{% for d in due %}<div style="padding:6px 0;border-bottom:1px solid var(--line)">{% if d.kind == 'task' %}<b>{{ d.next_step }}</b> · <a href="{{ url_for('company', cid=d.company_id) }}">{{ d.company_name }}</a> <span class="muted">due {{ d.due }}</span>
{% else %}<b>{{ d.channel|capitalize }}</b> {{ d.name }} <span class="muted">({{ d.title or '' }}, {{ d.company_name or '' }})</span> <a href="{{ url_for('pipeline_page') }}#due">open</a>{% endif %}</div>{% endfor %}</div>{% endif %}
{% for r in rows %}<div class="card zm-card" style="margin-bottom:12px">
<div style="display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap">
<div class="zm-score" style="min-width:44px;text-align:center"><strong style="font-size:22px">{{ r.score.total }}</strong><br><span class="muted" style="font-size:11px">score</span></div>
<div style="flex:1;min-width:240px">
<div><a href="{{ url_for('company', cid=r.company_id) }}"><b style="font-size:16px">{{ r.company }}</b></a> <span class="chip zm-score-in">score {{ r.score.total }}</span>
{% if r.due %} <span class="chip hm">call-back due</span>{% endif %}{% if r.stage %} <span class="chip">{{ r.stage }}</span>{% endif %}</div>
{{ cosummary(r.about) }}
{% if r.person %}<div style="margin-top:4px"><b>{{ r.person.name }}</b> · {{ r.person.title or '' }} <span class="muted">({{ r.why_person }})</span></div>
{% elif r.lead %}<div style="margin-top:4px">Ask for <b>{{ r.lead_label }}</b> <span class="muted">(main line; last name hidden until looked up)</span>
<div style="margin-top:4px">{% set left = apollo_left() %}{% if left is not none and not r.lead.enriched_at %}<form class="fb" method="post" action="{{ url_for('lead_enrich', lid=r.lead.id) }}" style="display:inline">
<button class="ghost" {{ 'disabled' if not left }} title="Uses one free Apollo credit; personal emails and phones are never requested">Get full name &amp; email</button></form>
<span class="muted" style="font-size:12px">{{ left }} free Apollo credit{{ '' if left == 1 else 's' }} left this month</span>{% elif r.lead.enriched_at and not r.lead.last_name %}<span class="muted" style="font-size:12px">· Apollo had no more on them</span>{% endif %}</div></div>
{% else %}<div style="margin-top:4px" class="muted">No named contact yet: call the main line and ask for the {{ r.ask_for }}.</div>{% endif %}
<div style="margin-top:2px;font-size:13px">{% if r.person %}<span class="muted">If they are the wrong person, ask for the</span> {{ r.ask_for }} · {% endif %}<a href="{{ li_search(r.ask_for, r.company) }}" target="_blank" rel="noopener">{{ 'find on LinkedIn' if r.person else 'Find the ' ~ r.ask_for ~ ' on LinkedIn' }}</a></div>
<div class="v zm-links" style="margin-top:4px">
<span class="zm-tel">{% if r.person and r.person.phone %}📞 <a href="tel:{{ r.person.phone }}">{{ r.person.phone|phone }}</a> <span class="muted">direct</span>{% if r.main_line %} · {% endif %}{% endif %}
{% if r.main_line %}{% if not (r.person and r.person.phone) %}📞 {% endif %}<a href="tel:{{ r.main_line }}">{{ r.main_line|phone }}</a> <span class="muted">main line</span>{% endif %}
{% if r.person and r.person.email %} · {% endif %}</span>
{% if not r.main_line and not (r.person and r.person.phone) %}<span class="muted">📞 no phone found</span>{% endif %}
{% if r.person and r.person.email %}✉ <a href="mailto:{{ r.person.email }}">{{ r.person.email }}</a>{% if r.person.email_status == 'guessed' %} <span class="muted">(guessed)</span>{% endif %}{% endif %}
{% if r.person %} · <a href="{{ r.person.linkedin_url or li_search(r.person.name, r.company) }}" target="_blank" rel="noopener">LinkedIn</a>{% endif %}</div>
{% if r.person %}{{ chanlist(r.channels, [r.person.phone, r.person.email]) }}{% endif %}
{% if r.person and (not r.person.email or r.person.email_status == 'guessed' or not r.person.phone) %}<div style="margin-top:4px">{{ loxobtn(r.person) }}</div>{% endif %}
{{ otherlist(r.others, r.company) }}
{% set num = (r.person.phone if r.person and r.person.phone else r.main_line) %}{% if num %}<a class="btn callbtn" href="tel:{{ num }}">📞 Call {{ num|phone }}</a>
<div class="muted zm-linekind">{{ 'direct line' if r.person and r.person.phone else 'main line' }}{% if num|linenote %} · <b>{{ num|linenote }}</b>{% endif %}</div>{% endif %}
<div style="margin-top:8px"><span class="muted">Why now:</span> {{ r.why_now|join(' · ') }}</div>
{{ roleslist(r.roles) }}
<div style="margin-top:8px"><span class="muted">Say:</span>
<div style="display:flex;gap:8px;align-items:flex-start;margin-top:4px"><div class="preview" style="flex:1;min-width:0">{{ r.opener }}</div>
<button type="button" class="ghost" style="background:transparent;color:var(--accent);border:1px solid var(--line);padding:4px 10px" data-text="{{ r.opener }}" onclick="navigator.clipboard.writeText(this.dataset.text);this.textContent='Copied'">Copy</button></div></div>
{% if r.pitches %}<div style="margin-top:8px"><span class="muted">Candidates to pitch:</span>
{% for m in r.pitches %}{% if loop.index == 2 %}<details class="zm-morec"><summary class="muted" style="cursor:pointer;font-size:13px;padding:6px 0">{{ r.pitches|length - 1 }} more candidate{{ 's' if r.pitches|length > 2 }}</summary>{% endif %}<div class="zm-cand" style="margin-left:8px">• <a href="{{ url_for('candidate', cid=m.c.id) }}">{{ m.c.name }}</a> <span class="{{ 'ol-warn chip' if m.stale else 'muted' }}" style="white-space:normal">{{ m.why }}</span> <span class="muted">· for {{ clean_role(m.job_title) }}</span>
· <a href="{{ url_for('pitch_page', candidate=m.c.id, job=m.job_id) }}">Pitch email</a></div>{% if loop.last and loop.length > 1 %}</details>{% endif %}{% endfor %}</div>
{% else %}<div class="muted" style="margin-top:8px;font-size:13px">No matching candidates in Loxo yet for these roles.</div>{% endif %}
{% if r.last %}<div class="muted" style="margin-top:6px;font-size:13px">Last touch: {{ r.last.body }} · {{ r.last.created_at[:10] }}</div>{% endif %}
{% if r.next_step %}<div class="muted" style="font-size:13px">Next step: {{ r.next_step }}</div>{% endif %}
<form class="zm-log" data-company="{{ r.company }}" method="post" action="{{ url_for('today_log') }}" style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap;align-items:center">
<input type="hidden" name="company" value="{{ r.company_id }}"><input type="hidden" name="person" value="{{ r.person.id if r.person else '' }}">
<input name="note" placeholder="Note (optional)" aria-label="Note about this call" style="flex:1;min-width:160px">
<button name="outcome" value="connected">Connected</button>
<button name="outcome" value="voicemail" class="ghost" style="background:transparent;color:var(--accent);border:1px solid var(--line)">Voicemail</button>
<button name="outcome" value="emailed" class="ghost" style="background:transparent;color:var(--accent);border:1px solid var(--line)">Emailed</button>
<button name="outcome" value="meeting">Meeting booked</button>
<details style="display:inline-block"><summary class="muted" style="cursor:pointer;font-size:13px;padding:6px 4px">More</summary>
<button name="outcome" value="no_answer" class="ghost" style="background:transparent;color:var(--accent);border:1px solid var(--line);margin-top:6px">No answer</button>
<button name="outcome" value="bad_number" class="ghost" style="background:transparent;color:var(--accent);border:1px solid var(--line);margin-top:6px" title="Tech support, disconnected or the wrong company: removes this number for good and shows the next one" onclick="return confirm('Remove the number shown for {{ r.company|e }}? It won\\'t come back when the site is read again. Type why in the note first if you like.')">Bad number</button>
<button name="outcome" value="not_interested" class="no" style="margin-top:6px" onclick="return confirm('Mark {{ r.company|e }} as not interested? It moves to Not now and leaves this list.')">Not interested</button></details></form>
</div></div></div>
{% else %}<div class="card"><p>Nobody to call right now.</p><p class="muted">Accounts appear here when they have open roles that fit <a href="{{ url_for('companies') }}">your desk</a> in your area. Find more on the <a href="{{ url_for('find') }}">Prospect</a> tab, or come back when call-backs are due.</p></div>{% endfor %}
{% if done %}<div class="section"><b>Logged today</b> <span class="muted">· {{ done|length }}</span></div>
<div class="wrap"><table><tr><th>When</th><th>Company</th><th>Person</th><th>Outcome</th></tr>
{% for d in done %}<tr><td>{{ d.time }}</td><td><a href="{{ url_for('company', cid=d.company_id) }}">{{ d.company_name or '' }}</a></td><td>{{ d.person_name or '' }}</td><td>{{ d.body }}</td></tr>{% endfor %}</table></div>{% endif %}
<div id="zm-toast" role="status" hidden><span></span>
<form class="fb" method="post" action="{{ url_for('today_undo') }}"><button class="ghost">Undo</button></form></div>
<script>
// Log a call without reloading the page: the card slides away and the next one moves up.
(function () {
  if (!window.fetch || !window.FormData) return;
  var toast = document.getElementById("zm-toast"), hide;
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.classList || !form.classList.contains("zm-log") || !e.submitter) return;
    e.preventDefault();
    if (form.dataset.sending) return;  // a second tap while the first is on its way
    form.dataset.sending = "1";
    var data = new FormData(form), card = form.closest(".zm-card"), btn = e.submitter, was = btn.textContent;
    data.set("outcome", btn.value);
    btn.textContent = "Saving…";
    fetch(form.action, {method: "POST", body: data, headers: {"X-Requested-With": "fetch"}})
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (j) {
        if (j.reload || document.querySelectorAll(".zm-card").length <= 1) { location.reload(); return; }
        card.style.transition = "opacity .25s, max-height .3s"; card.style.overflow = "hidden";
        card.style.maxHeight = card.offsetHeight + "px"; card.style.opacity = "0";
        setTimeout(function () { card.style.maxHeight = "0"; card.style.padding = "0"; card.style.margin = "0"; card.style.border = "0"; }, 30);
        setTimeout(function () { card.remove(); }, 380);
        var n = document.querySelectorAll(".zm-card").length - 1;
        document.getElementById("zm-left").textContent = n + " account" + (n === 1 ? "" : "s") + " to call";
        var tabn = document.querySelector("nav a.on .n"); if (tabn) tabn.textContent = n;
        if (j.prog && document.getElementById("zm-dials")) {
          document.getElementById("zm-dials").textContent = j.prog.dials + " of " + j.prog.goal;
          document.getElementById("zm-bar").style.width = j.prog.pct + "%";
        }
        toast.firstChild.textContent = "Logged " + j.label + " for " + j.company + ".";
        toast.hidden = false; clearTimeout(hide); hide = setTimeout(function () { toast.hidden = true; }, 20000);
      })
      .catch(function () {  // no connection: say so and let the tap be tried again
        delete form.dataset.sending; btn.textContent = was;
        toast.firstChild.textContent = "Couldn't save that. Check your connection and tap again.";
        toast.hidden = false;
      });
  });
})();
</script>"""

BLITZ_BUILD = """<h2>Call blitz</h2>
<div class="sub">Build a list, press Start, and work it one card at a time. Every outcome is logged like it is on Today (pipeline, Scoreboard and Loxo).</div>
{% if session %}<div class="preview" role="status" style="margin-bottom:14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
<span>A blitz is in progress: <b>{{ session.name }}</b>, card {{ [session.i + 1, session['items']|length]|min }} of {{ session['items']|length }}.</span>
<a class="btn" href="{{ url_for('blitz_run') }}">Resume</a>
<form class="fb" method="post" action="{{ url_for('blitz_end') }}"><button class="ghost">End it</button></form></div>{% endif %}
<div class="grid">
<form class="card big" method="post" action="{{ url_for('blitz_start') }}"><h3>Who to call</h3>
<div class="row" style="gap:16px;margin-bottom:10px">
<label><input type="radio" name="source" value="priority" {{ 'checked' if f.source == 'priority' }} style="padding:0;width:auto;flex:none;min-width:0;margin:0 4px 0 0" onchange="zmSrc()"> <b>Today's priorities</b> <span class="muted">· the same order as Today</span></label>
<label><input type="radio" name="source" value="custom" {{ 'checked' if f.source == 'custom' }} style="padding:0;width:auto;flex:none;min-width:0;margin:0 4px 0 0" onchange="zmSrc()"> <b>Custom filters</b></label></div>
<button name="action" value="start" class="zm-phone" style="font-size:17px;padding:14px;width:100%;margin-bottom:14px">▶ Start blitz ({{ f.size }} calls)</button>
<div id="zm-custom" class="grid" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px">
<label class="muted" style="font-size:12px">Vertical<select name="vertical" style="width:100%;margin-top:2px"><option value="">All</option>{% for k, v in verticals.items() %}<option value="{{ k }}" {{ 'selected' if f.vertical == k }}>{{ v[0] }}</option>{% endfor %}</select></label>
<label class="muted" style="font-size:12px">City contains<input name="city" value="{{ f.city }}" placeholder="Frisco" style="width:100%;margin-top:2px"></label>
<label class="muted" style="font-size:12px">Call<select name="who" style="width:100%;margin-top:2px">{% for k, v in who.items() %}<option value="{{ k }}" {{ 'selected' if f.who == k }}>{{ v }}</option>{% endfor %}</select></label>
<label class="muted" style="font-size:12px">Pipeline stage<select name="stage" style="width:100%;margin-top:2px">{% for k, v in stages.items() %}<option value="{{ k }}" {{ 'selected' if f.stage == k }}>{{ v }}</option>{% endfor %}</select></label>
<label class="muted" style="font-size:12px">Minimum score<input name="min_score" type="number" min="0" value="{{ f.min_score }}" style="width:100%;margin-top:2px"></label>
<label class="muted" style="font-size:12px">Skip if touched in the last … business days<input name="rest_days" type="number" min="0" value="{{ f.rest_days }}" style="width:100%;margin-top:2px"></label>
<label class="muted" style="font-size:12px">Order<select name="sort" style="width:100%;margin-top:2px">{% for k, v in sorts.items() %}<option value="{{ k }}" {{ 'selected' if f.sort == k }}>{{ v }}</option>{% endfor %}</select></label>
<div style="display:flex;flex-direction:column;gap:6px;justify-content:flex-end">
<label class="muted" style="font-size:13px"><input type="checkbox" name="fit_only" value="1" {{ 'checked' if f.fit_only }} style="padding:0"> Only accounts with roles that fit my desk</label>
<label class="muted" style="font-size:13px"><input type="checkbox" name="direct" value="1" {{ 'checked' if f.direct }} style="padding:0"> Direct dials only</label></div></div>
<div class="row" style="margin-top:14px;align-items:flex-end">
<label class="muted" style="font-size:12px">How many<input name="size" type="number" min="1" max="200" value="{{ f.size }}" style="width:90px;margin-top:2px"></label>
<button name="action" value="start" style="font-size:16px;padding:10px 22px">▶ Start blitz</button>
<button name="action" value="preview" class="ghost" style="background:transparent;color:var(--accent);border:1px solid var(--line)">Preview list</button>
<input name="list_name" placeholder="Name this list to save it" style="max-width:220px">
<button name="action" value="save" class="ghost" style="background:transparent;color:var(--accent);border:1px solid var(--line)">Save list</button></div></form>
<div class="card"><h3>Saved lists</h3>
{% for name, sf in saved.items() %}<div style="display:flex;gap:8px;align-items:center;padding:6px 0;border-bottom:1px solid var(--line)">
<div style="flex:1"><b>{{ name }}</b><br><span class="muted" style="font-size:12px">{{ describe(sf) }}</span></div>
<form class="fb" method="post" action="{{ url_for('blitz_start') }}"><input type="hidden" name="saved" value="{{ name }}"><button name="action" value="start">▶ Start</button></form>
<form class="fb" method="post" action="{{ url_for('blitz_start') }}"><input type="hidden" name="saved" value="{{ name }}"><button name="action" value="delete" class="no" onclick="return confirm('Delete the saved list {{ name|e }}?')">Delete</button></form></div>
{% else %}<p class="muted">None yet. Set filters, name the list and press Save list. Saved lists re-run fresh each time, so they pick up new accounts.</p>{% endfor %}
<h3 style="margin-top:16px">Import a list (CSV)</h3>
<form method="post" action="{{ url_for('blitz_import') }}" enctype="multipart/form-data">
<p class="muted" style="font-size:13px;margin:0 0 6px">From Apollo, ZoomInfo, Sales Navigator, Loxo or your own sheet. You'll confirm the columns before anything is saved.</p>
<input type="file" name="file" accept=".csv,.txt,.tsv,text/csv" required style="width:100%;margin-bottom:8px">
<button>Upload</button></form>
<h3 style="margin-top:16px">Dialing</h3>
<form method="post" action="{{ url_for('blitz_settings') }}">
<label class="muted" style="font-size:12px">Dial with<select name="dialer" style="width:100%;margin:2px 0 8px">
<option value="gv" {{ 'selected' if st.dialer == 'gv' }}>Google Voice (opens voice.google.com)</option>
<option value="tel" {{ 'selected' if st.dialer == 'tel' }}>This PC's phone app (tel: links)</option></select></label>
<label class="muted" style="font-size:12px">Google account number <span class="muted">(0 unless Google Voice is on a second Google account you're signed in to)</span><input name="gv_account" value="{{ st.gv_account }}" style="width:70px;margin:2px 0 8px;display:block"></label>
<label class="muted" style="font-size:12px">Your call-back number <span class="muted">(read out in voicemails)</span><input name="my_phone" value="{{ st.my_phone }}" placeholder="(214) 555-0100" style="width:100%;margin:2px 0 8px"></label>
<button>Save</button></form></div></div>
{% if preview is not none %}<div class="section"><b>Preview</b> <span class="muted">· {{ preview|length }} account{{ 's' if preview|length != 1 }} · {{ describe(f) }}</span></div>
<div class="wrap"><table><tr><th>#</th><th>Company</th><th>Calling</th><th>Number</th></tr>
{% for p in preview %}<tr><td>{{ loop.index }}</td><td><a href="{{ url_for('company', cid=p.company_id) }}">{{ p.company }}</a></td><td>{{ p.person or 'front desk' }}</td><td>{{ p.phone|phone }} <span class="muted">{{ p.kind }}</span></td></tr>
{% else %}<tr><td colspan="4">Nobody matches. Loosen a filter: lower the minimum score, untick "direct dials only", or set the skip days to 0.</td></tr>{% endfor %}</table></div>{% endif %}
<script>function zmSrc(){var c=document.querySelector('input[name=source]:checked').value==='custom';
document.getElementById('zm-custom').style.opacity=c?1:.45;document.querySelectorAll('#zm-custom select,#zm-custom input').forEach(function(e){e.disabled=!c});}zmSrc();</script>"""

BLITZ_RUN = PHONE_MACRO + ROLES_MACRO + LOXO_MACRO + CHANNEL_MACRO + """<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px">
<h2 style="margin:0">Blitz: {{ s.name }}</h2>
<span class="chip">card {{ s.i + 1 }} of {{ s['items']|length }}</span>
<span style="font-size:14px"><b>{{ t.dials }}</b> dial{{ 's' if t.dials != 1 }} · <b>{{ t.conversations }}</b> conversation{{ 's' if t.conversations != 1 }} · <b>{{ t.voicemail }}</b> voicemail{{ 's' if t.voicemail != 1 }} · <b>{{ t.meeting }}</b> meeting{{ 's' if t.meeting != 1 }}{% if t.meeting %} 🎉{% endif %} · <span id="zm-clock" class="muted" data-start="{{ s.started }}"></span></span>
<form class="fb" method="post" action="{{ url_for('blitz_end') }}" style="margin-left:auto"><button class="ghost" style="background:transparent;color:var(--accent);border:1px solid var(--line)">End blitz</button></form></div>
<div role="progressbar" aria-label="Blitz progress" aria-valuenow="{{ s.i }}" aria-valuemin="0" aria-valuemax="{{ s['items']|length }}" style="height:6px;background:var(--seg);border-radius:4px;overflow:hidden;margin-bottom:14px"><div style="height:100%;width:{{ (100 * s.i / s['items']|length)|round }}%;background:var(--accent)"></div></div>
{% if last %}<div class="preview" role="status" style="margin-bottom:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:6px 10px">
<span>{% if last.outcome == 'skip' %}Skipped{% else %}Logged <b>{{ labels[last.outcome] }}</b> for{% endif %} {{ last.company }}.</span>
<form class="fb" method="post" action="{{ url_for('blitz_undo') }}"><button class="ghost" style="padding:3px 10px">Undo <kbd>U</kbd></button></form></div>{% endif %}
<div class="card zm-run" style="padding:20px">
<div style="display:flex;gap:20px;flex-wrap:wrap;align-items:flex-start">
<div style="flex:1;min-width:260px">
<div><a href="{{ url_for('company', cid=c.company_id) }}" target="_blank" rel="noopener"><b style="font-size:22px">{{ c.company }}</b></a>
{% if c.score %}<span class="chip">score {{ c.score }}</span>{% endif %}{% if c.stage %} <span class="chip">{{ c.stage }}</span>{% endif %}</div>
{{ cosummary(c.about) }}
{% if c.person %}<div style="margin-top:6px;font-size:17px"><b>{{ c.person.name }}</b> · {{ c.person.title or '' }} <span class="muted" style="font-size:13px">({{ c.why_person }})</span>
· <a href="{{ c.person.linkedin_url or li_search(c.person.name, c.company) }}" target="_blank" rel="noopener" style="font-size:13px">LinkedIn</a>
{% if not c.person.email or c.person.email_status == 'guessed' or not c.person.phone %} {{ loxobtn(c.person) }}{% endif %}
{{ chanlist(c.channels, [c.phone]) }}</div>
{% elif c.lead %}<div style="margin-top:6px;font-size:16px">Front desk · ask for <b>{{ c.lead_label }}</b>
<span class="muted" style="font-size:13px">(last name hidden)</span><div style="margin-top:4px;font-size:14px">{% set left = apollo_left() %}{% if left is not none and not c.lead.enriched_at %}<form class="fb" method="post" action="{{ url_for('lead_enrich', lid=c.lead.id) }}" style="display:inline">
<button class="ghost" {{ 'disabled' if not left }} title="Uses one free Apollo credit; personal emails and phones are never requested">Get full name &amp; email</button></form>
<span class="muted" style="font-size:12px">{{ left }} free Apollo credit{{ '' if left == 1 else 's' }} left this month</span>{% elif c.lead.enriched_at and not c.lead.last_name %}<span class="muted" style="font-size:12px">· Apollo had no more on them</span>{% endif %}</div></div>
{% else %}<div style="margin-top:6px;font-size:16px">Front desk · ask for the <b>{{ c.ask_for }}</b> · <a href="{{ li_search(c.ask_for, c.company) }}" target="_blank" rel="noopener" style="font-size:13px">find them on LinkedIn</a></div>{% endif %}
{{ otherlist(c.others, c.company) }}
{% if c.why_now %}<div style="margin-top:8px;font-size:14px"><span class="muted">Why now:</span> {{ c.why_now|join(' · ') }}</div>{% endif %}
{{ roleslist(c.roles) }}
{% if c.last %}<div class="muted" style="margin-top:4px;font-size:13px">Last touch: {{ c.last.body }} · {{ c.last.created_at[:10] }}{% if c.next_step %} · next step: {{ c.next_step }}{% endif %}</div>{% endif %}</div>
<div style="text-align:center;min-width:240px">
{% if c.phone %}<div class="zm-num" style="font-size:30px;font-weight:700;letter-spacing:.02em">{{ c.phone|phone }}</div>
<div class="muted" style="font-size:13px">{{ c.kind }}{% if c.phone|linenote %} · <b>{{ c.phone|linenote }}</b>{% endif %}{% if c.ext %} · then dial ext. <b>{{ c.ext }}</b>{% endif %}{% if c.kind == 'direct' and c.main_line %} · main {{ c.main_line|phone }}{% endif %}</div>
{% if st.dialer == 'gv' %}<button type="button" id="zm-dial" class="zm-dialbtn" data-url="{{ c.gv }}" style="margin-top:10px;font-size:18px;padding:12px 28px">📞 Call with Google Voice <kbd>D</kbd></button>
<div class="zm-pc" style="margin-top:6px;font-size:12px"><a href="tel:{{ c.phone }}">or this PC's phone app</a></div>
<a class="btn zm-phone zm-dialbtn" href="tel:{{ c.phone|replace(' x', ',') }}">📱 Call from phone dialer</a>
{% else %}<a class="btn zm-dialbtn" id="zm-dial" href="tel:{{ c.phone|replace(' x', ',') }}" style="margin-top:10px;font-size:18px;padding:12px 28px;display:inline-block">📞 Call <kbd>D</kbd></a>{% endif %}
<div style="margin-top:6px"><button type="button" class="ghost" style="background:transparent;color:var(--muted);border:1px solid var(--line);font-size:13px"
onclick="if(confirm('Remove this number for {{ c.company|e }}? It will not come back. The card stays so you can try their next number.')){document.getElementById('zm-outcome').value='bad_number';document.getElementById('zm-log').submit()}">Bad number (tech support, wrong company, disconnected)</button></div>
{% else %}<div class="muted">No number for this one. Skip it.</div>{% endif %}</div></div>
<div style="margin-top:16px"><span class="muted">Say:</span>
<div class="preview" style="font-size:17px;line-height:1.5;margin-top:4px">{{ c.opener }}</div></div>
{% if c.pitches %}<div style="margin-top:10px;font-size:14px"><span class="muted">Candidates to pitch:</span>
{% for m in c.pitches %}<div style="margin-left:8px">• <a href="{{ url_for('candidate', cid=m.c.id) }}" target="_blank" rel="noopener">{{ m.c.name }}</a> <span class="{{ 'ol-warn chip' if m.stale else 'muted' }}" style="white-space:normal">{{ m.why }}</span> <span class="muted">· for {{ clean_role(m.job_title) }}</span></div>{% endfor %}</div>{% endif %}
<div class="grid" style="margin-top:12px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px">
<div><span class="muted" style="font-size:13px">If you get voicemail:</span><div class="preview" style="font-size:14px;margin-top:4px">{{ c.voicemail }}</div></div>
<div><span class="muted" style="font-size:13px">If you get the front desk:</span><div class="preview" style="font-size:14px;margin-top:4px">{{ c.front_desk }}</div></div></div>
<details style="margin-top:10px"><summary class="muted" style="cursor:pointer;font-size:14px">If they push back <kbd>P</kbd></summary>
{% for q, a in c.objections %}<div style="margin-top:8px;font-size:14px"><b>“{{ q }}”</b><br>{{ a }}</div>{% endfor %}</details>
<form id="zm-log" class="zm-sticky" method="post" action="{{ url_for('blitz_log') }}" style="margin-top:16px;display:flex;gap:6px;flex-wrap:wrap;align-items:center">
<input type="hidden" name="i" value="{{ s.i }}"><input type="hidden" name="outcome" id="zm-outcome" value="">
<input name="note" id="zm-note" placeholder="Note (optional)" aria-label="Note about this call" style="flex:1;min-width:200px">
<button type="submit" data-o="connected">Connected <kbd>C</kbd></button>
<button type="submit" data-o="voicemail" class="ghost" style="background:transparent;color:var(--accent);border:1px solid var(--line)">Voicemail <kbd>V</kbd></button>
<button type="submit" data-o="no_answer" class="ghost" style="background:transparent;color:var(--accent);border:1px solid var(--line)">No answer <kbd>N</kbd></button>
<button type="submit" data-o="meeting">Meeting booked <kbd>M</kbd></button>
<button type="submit" data-o="not_interested" class="no">Not interested <kbd>X</kbd></button>
<button type="submit" data-o="skip" class="ghost" style="background:transparent;color:var(--muted);border:1px solid var(--line)">Skip <kbd>S</kbd></button></form>
<div class="muted zm-pc" style="font-size:12px;margin-top:8px">Voicemail and No answer set a call-back in 2 business days. Not interested moves the account to Not now. Undo with <kbd>U</kbd>.</div></div>
<form id="zm-undo" method="post" action="{{ url_for('blitz_undo') }}" hidden></form>
<style>kbd{font:11px/1 ui-monospace,monospace;border:1px solid var(--line);border-bottom-width:2px;border-radius:4px;padding:2px 5px;margin-left:4px;opacity:.8}
.zm-phone{display:none}
/* Phone: no keyboard hints, full-width call buttons, big outcome buttons two to a row */
html.mobile button.zm-phone{display:block}
html.mobile kbd,html.mobile .zm-pc,html.mobile .zm-keyhint{display:none}
html.mobile .zm-dialbtn{display:block;width:100%;margin-top:10px;font-size:18px;padding:14px;text-align:center;border-radius:12px}
html.mobile a.zm-dialbtn{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600}
html.mobile #zm-log{display:grid!important;grid-template-columns:1fr 1fr 1fr;gap:6px}
html.mobile #zm-log input[name=note]{grid-column:1/-1;min-width:0}
html.mobile #zm-log button{padding:12px 2px;font-size:14px;border-radius:10px;min-height:46px}
html.mobile #zm-log input[name=note]{padding:8px 10px}
html.mobile .zm-num{font-size:26px!important}</style>
<script>
(function () {
  var form = document.getElementById("zm-log"), out = document.getElementById("zm-outcome"),
      note = document.getElementById("zm-note"), dial = document.getElementById("zm-dial"), sent = false;
  function submit(o) { if (sent) return; sent = true; out.value = o; form.submit(); }
  form.querySelectorAll("button[data-o]").forEach(function (b) {
    b.addEventListener("click", function (e) { e.preventDefault(); submit(b.dataset.o); });
  });
  form.addEventListener("submit", function (e) { if (!out.value) e.preventDefault(); });  // Enter in the note box
  function doDial() {
    if (!dial) return;
    if (dial.dataset.url) window.open(dial.dataset.url, "zm_gvoice"); else window.location.href = dial.href;
  }
  if (dial && dial.dataset.url) dial.addEventListener("click", doDial);
  var keys = {c: "connected", v: "voicemail", n: "no_answer", m: "meeting", x: "not_interested", s: "skip"};
  document.addEventListener("keydown", function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    var typing = /INPUT|TEXTAREA|SELECT/.test(e.target.tagName);
    if (typing) { if (e.key === "Escape") e.target.blur(); return; }
    var k = e.key.toLowerCase();
    if (k === "d") { e.preventDefault(); doDial(); }
    else if (keys[k]) { e.preventDefault(); submit(keys[k]); }
    else if (k === "u") { e.preventDefault(); document.getElementById("zm-undo").submit(); }
    else if (k === "/") { e.preventDefault(); note.focus(); }
    else if (k === "p") { var d = document.querySelector("details"); if (d) d.open = !d.open; }
  });
  var clock = document.getElementById("zm-clock"), start = Date.parse(clock.dataset.start);
  function tick() { var m = Math.max(0, Math.floor((Date.now() - start) / 60000));
    clock.textContent = (m >= 60 ? Math.floor(m / 60) + " h " : "") + (m % 60) + " min"; }
  tick(); setInterval(tick, 30000);
})();
</script>"""

BLITZ_DONE = """<h2>Blitz done: {{ s.name }}</h2>
<div class="sub">{{ s['items']|length }} card{{ 's' if s['items']|length != 1 }} · {{ minutes }} min</div>
<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px">
{% for label, n in [('Dials', t.dials), ('Conversations', t.conversations), ('Connect rate', t.connect_rate ~ '%'), ('Voicemails', t.voicemail), ('No answer', t.no_answer), ('Meetings booked', t.meeting), ('Not interested', t.not_interested), ('Skipped', t.skipped)] %}
<div class="card" style="text-align:center"><div style="font-size:28px;font-weight:700">{{ n }}</div><div class="muted" style="font-size:13px">{{ label }}</div></div>{% endfor %}</div>
<div class="row" style="margin-bottom:14px"><a class="btn" href="{{ url_for('blitz_page') }}">▶ Start another blitz</a>
<a class="btn" href="{{ url_for('today_page') }}" style="background:transparent;color:var(--accent);border:1px solid var(--line)">Back to Today</a>
<a class="btn" href="{{ url_for('scoreboard_page') }}" style="background:transparent;color:var(--accent);border:1px solid var(--line)">Scoreboard</a></div>
{% if s.log %}<div class="wrap"><table><tr><th>When</th><th>Company</th><th>Person</th><th>Outcome</th><th>Note</th></tr>
{% for e in s.log %}<tr><td>{{ e.time }}</td><td><a href="{{ url_for('company', cid=e.company_id) }}">{{ e.company }}</a></td><td>{{ e.person or 'front desk' }}</td>
<td>{{ 'Skipped' if e.outcome == 'skip' else labels[e.outcome] }}</td><td class="wraptext">{{ e.note or '' }}</td></tr>{% endfor %}</table></div>{% endif %}"""

BLITZ_MAP = """<h2>Import {{ filename }}</h2>
<div class="sub">{{ total }} row{{ 's' if total != 1 }}{% if capped %} (only the first {{ total }} are read){% endif %}. Check which column is which, then save.</div>
<form method="post" action="{{ url_for('blitz_import_apply') }}">
<div class="wrap"><table><tr><th>Zoomollo field</th><th>Column in your file</th><th>First rows</th></tr>
{% for f, label in fields %}<tr><td><b>{{ label }}</b></td>
<td><select name="map_{{ f }}"><option value="">— not in this file —</option>{% for h in headers %}<option value="{{ loop.index0 }}" {{ 'selected' if mapping[f] == loop.index0 }}>{{ h }}</option>{% endfor %}</select></td>
<td class="wraptext muted" style="font-size:13px">{% if mapping[f] is not none %}{{ samples[f]|join(' · ') }}{% endif %}</td></tr>{% endfor %}</table></div>
<p class="muted" style="font-size:13px">You need a company or website on each row. A name, title and phone make the call cards useful.</p>
<div class="row" style="margin-top:10px">
<label class="muted" style="font-size:12px;flex:1;min-width:200px">What these companies do <span class="muted">(for new companies, e.g. "CPA firms")</span><input name="what" style="width:100%;margin-top:2px"></label>
<label class="muted" style="font-size:12px;flex:1;min-width:200px">City when the file has none<input name="city" placeholder="Dallas, TX" style="width:100%;margin-top:2px"></label></div>
<label class="muted" style="font-size:13px;display:block;margin-top:10px"><input type="checkbox" name="crawl" value="1" checked style="padding:0;width:auto"> Also read new companies' websites for more people and open roles (runs in the background)</label>
<div class="row" style="margin-top:14px">
<button name="then" value="blitz" style="font-size:16px;padding:10px 22px">Save and ▶ start blitz</button>
<button name="then" value="save" class="ghost" style="background:transparent;color:var(--accent);border:1px solid var(--line)">Save only</button>
<a class="btn" href="{{ url_for('blitz_page') }}" style="background:transparent;color:var(--muted);border:1px solid var(--line)">Cancel</a></div></form>"""

BLITZ_IMPORTED = """<h2>Imported {{ r.filename }}</h2>
<div class="sub">{{ r.rows }} row{{ 's' if r.rows != 1 }} read</div>
<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px">
{% for label, n in [('New companies', r.companies_new), ('Already in Zoomollo', r.companies_matched), ('New people', r.people_new), ('People already saved', r.people_matched), ('Ready to call', r['items']|length), ('Left out', r.skipped|length)] %}
<div class="card" style="text-align:center"><div style="font-size:28px;font-weight:700">{{ n }}</div><div class="muted" style="font-size:13px">{{ label }}</div></div>{% endfor %}</div>
<div class="row" style="margin-bottom:14px">{% if r['items'] %}<form class="fb" method="post" action="{{ url_for('blitz_import_start') }}"><button style="font-size:16px;padding:10px 22px">▶ Start blitz with these {{ r['items']|length }}</button></form>{% endif %}
<a class="btn" href="{{ url_for('blitz_page') }}" style="background:transparent;color:var(--accent);border:1px solid var(--line)">Back to Call blitz</a>
<a class="btn" href="{{ url_for('companies') }}" style="background:transparent;color:var(--accent);border:1px solid var(--line)">Accounts</a></div>
{% if r.crawling %}<p class="muted">Reading {{ r.crawling }} new compan{{ 'ies' if r.crawling != 1 else 'y' }}' websites in the background.</p>{% endif %}
{% if r.skipped %}<details class="card"><summary><b>Left out of the call list</b> <span class="muted">· {{ r.skipped|length }}</span></summary>
<div class="wrap" style="margin-top:8px"><table><tr><th>Row</th><th>Why</th></tr>{% for n, why in r.skipped[:300] %}<tr><td>{{ n }}</td><td class="wraptext">{{ why }}</td></tr>{% endfor %}</table></div>
{% if r.skipped|length > 300 %}<p class="muted">…and {{ r.skipped|length - 300 }} more.</p>{% endif %}</details>{% endif %}"""

DESK_CARD ="""<details class="card" {{ 'open' if not desk.is_set }} style="margin-bottom:14px"><summary><b>My desk</b>
<span class="muted">{% if desk.is_set %}· {{ desk.titles|length if desk.titles else 'any' }} role{{ 's' if desk.titles|length != 1 }}{% if desk.locations %} · {{ desk.locations|length }} area{{ 's' if desk.locations|length != 1 }} ({{ desk.locations[:3]|map('replace', ', TX', '')|join(', ') }}{{ '…' if desk.locations|length > 3 }}){% endif %}{% if desk.min_salary %} · ${{ '{:,}'.format(desk.min_salary) }}+{% endif %} · <span style="color:var(--accent)">Edit</span>{% else %}· set your niche so the score focuses on roles you place{% endif %}</span></summary>
<form method="post" action="{{ url_for('desk_post') }}" style="margin-top:10px"><div class="grid">
<label>Roles you place <span class="muted">(comma or one per line)</span><textarea name="titles" style="width:100%;min-height:70px" placeholder="HVAC technician, service manager, estimator">{{ desk.titles|join(', ') }}</textarea></label>
<label>Roles to skip<textarea name="exclude" style="width:100%;min-height:70px" placeholder="intern, driver">{{ desk.exclude|join(', ') }}</textarea></label>
<label>Locations you cover <span class="muted">(blank = anywhere)</span><textarea name="locations" style="width:100%;min-height:70px" placeholder="Austin, TX; Dallas, TX; MO; remote">{{ desk.locations|join('; ') }}</textarea></label>
<label>Minimum salary <span class="muted">(only used when a post states pay)</span><input name="min_salary" value="{{ desk.min_salary or '' }}" placeholder="60000" style="width:100%"></label>
<label>Skip companies with more than this many open roles <span class="muted">(rough company size; national giants like BDO and KPMG are always skipped)</span><input name="max_open_roles" value="{{ desk.max_open_roles or '' }}" placeholder="75" style="width:100%"></label>
<label>Company size you target <span class="muted">(total employees; a company a little outside still counts, and unknown sizes are kept)</span>
<span class="row"><input name="min_headcount" value="{{ desk.min_headcount or '' }}" placeholder="100" style="width:90px" aria-label="Smallest headcount"> to
<input name="max_headcount" value="{{ desk.max_headcount or '' }}" placeholder="500" style="width:90px" aria-label="Largest headcount"> people</span></label>
</div><button style="margin-top:10px">Save my desk</button></form></details>"""

COMPANIES = SCORE_MACRO + OL_MACRO + PHONE_MACRO + """{{ desk_card|safe }}
<form class="bar"><input type="search" name="q" value="{{ q }}" placeholder="Company, city, industry…">
<label class="chip" style="padding:8px 10px"><input type="checkbox" name="niche" value="1" {{ 'checked' if niche }} style="padding:0"> Only with roles in my niche</label><button>Search</button></form>
<div class="count">{{ count_line }}</div>
<div class="wrap"><table><tr><th>Score</th><th>Company</th><th>Phone</th><th>Why</th><th>Roles</th><th>People</th><th>Stage</th></tr>
{% for sc, c in rows %}<tr><td>{{ scorechip(sc) }}</td>
<td><a href="{{ url_for('company', cid=c.id) }}"><b>{{ c.name or c.key }}</b></a>{% if c.city %}<br><span class="muted">{{ c.city }}</span>{% endif %}</td>
<td>{{ phonecell(cophone(c.id)) }}</td>
<td class="wraptext why">{% if sc.offlimits.blocked %}{{ olbadge(sc.offlimits) }} {{ sc.offlimits.reason }}{% else %}{{ sc.top_reasons|join(' · ') or 'no open roles found' }}{% endif %}</td>
<td>{% if sc.fit_roles %}<span class="chip fit">{{ sc.fit_roles|length }} fit</span>{% endif %}{% if sc.other_roles %} <span class="chip">{{ sc.other_roles }} other</span>{% endif %}</td>
<td>{{ counts.get(c.id, 0) }}</td>
<td>{% if sc.stage %}<span class="chip">{{ stage_names[sc.stage] }}</span>{% elif not sc.offlimits.blocked and sc.total %}<form class="fb" method="post" action="{{ url_for('pipeline_post', cid=c.id) }}"><input type="hidden" name="action" value="add"><button class="ghost">+ Pipeline</button></form>{% endif %}</td></tr>
{% else %}<tr><td colspan="7">{% if niche %}No companies with open roles in your niche yet.{% else %}No companies yet.{% endif %}</td></tr>{% endfor %}</table></div>"""

CONTACTS = PHONE_MACRO + """<form class="bar"><input type="search" name="q" value="{{ q }}" placeholder="Search name, email, title, company…">
<select name="type"><option value="">Emails + phones</option>{% for t in ['email','phone'] %}<option {{ 'selected' if t==type_ }}>{{ t }}</option>{% endfor %}</select>
<select name="kind"><option value="">Any kind</option>{% for k in ['person','company','guessed'] %}<option {{ 'selected' if k==kind }}>{{ k }}</option>{% endfor %}</select>
<button>Search</button><a class="btn" href="{{ url_for('export', q=q, type=type_, kind=kind) }}">Export CSV</a></form>
<div class="count">{{ count_line }}</div>
<div class="wrap"><table><tr><th>Company</th><th>Name</th><th>Title</th><th>Contact</th><th>Phone</th><th>Kind</th><th>Source</th></tr>
{% for r in rows %}<tr><td><a href="{{ url_for('company', cid=r.company_id) }}">{{ r.company_name or r.company_key or '' }}</a></td><td>{{ r.name or '' }}</td><td>{{ r.title or '' }}</td>
<td class="v">{{ r.value }}</td><td>{{ phonecell(r.value if r.type == 'phone' else None, cophone(r.company_id)) }}</td><td><span class="chip">{{ r.kind }}</span></td>
<td>{% if r.source_url and r.source_url.startswith('http') %}<a href="{{ r.source_url }}" target="_blank" rel="noopener">page</a>{% endif %}</td></tr>
{% else %}<tr><td colspan="7">Nothing yet.</td></tr>{% endfor %}</table></div>"""

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
{% for r in queue %}<tr id="q{{ r.p.id }}"><td><b>{{ r.p.name }}</b><br><span class="muted">{{ r.p.title or '' }} · {{ r.p.company_name or '' }}</span><br><span class="v">{{ r.p.phone|phone }}</span></td>
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

MESSAGE_ONE = """<h2>Text {{ who }}</h2><div class="sub">{{ phone|phone }}{% if last %} · last texted {{ last[:10] }}{% endif %}</div>
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
<p class="muted" style="font-size:13px;margin-top:8px">Replies arrive on your phone, not in Zoomollo. If someone answers STOP (or "remove me"), click the button above so they're never texted again. Texts can only be opened between 8am and 9pm.</p>
<script>
function zmCopy(b){var t=document.getElementById("msgtext").value;
  (navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(function(){b.textContent="Copied ✓"},function(){var a=document.getElementById("msgtext");a.select();document.execCommand("copy");b.textContent="Copied ✓"});}
var zmT;function zmQR(){clearTimeout(zmT);zmT=setTimeout(function(){
  fetch("{{ url_for('message_qr') }}",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},
   body:"phone="+encodeURIComponent({{ phone|tojson }})+"&text="+encodeURIComponent(document.getElementById("msgtext").value)})
  .then(function(r){return r.text()}).then(function(svg){document.getElementById("qr").innerHTML=svg});},400);}
</script>{% endif %}
{% if history %}<div class="section"><b>Sent before</b></div>{% for m in history %}<div class="preview" style="margin-bottom:8px"><span class="muted">{{ m.sent_at[:16].replace('T',' ') }} · {{ m.channel }}</span><br>{{ m.body }}</div>{% endfor %}{% endif %}"""

PIPELINE = OL_MACRO + SCORE_MACRO + PHONE_MACRO + """<details class="zm-fold" open><summary>Email, auto-prospecting{{ ' (on)' if auto and auto.cfg.on else ' (off)' if auto else '' }}, Loxo and phone settings</summary><div class="grid">
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
<div class="card" id="autosend"><h3>Auto-prospecting</h3>
{% if auto.msg %}<div class="preview" style="margin-bottom:8px">{{ auto.msg }}</div>{% endif %}
<p>{% if auto.cfg.on %}<b>On.</b>{% else %}<b>Off.</b>{% endif %} Emails up to <b>{{ auto.cfg.per_hour }}</b> of your best actively-hiring prospects every hour,
{{ 'weekdays ' if auto.cfg.weekdays }}{{ auto.start }}–{{ auto.end }}, then sends their follow-ups. One person per company; replies, unsubscribes and bounces stop it. Pauses itself if too many guessed emails bounce.</p>
{% if auto.blockers %}<div class="warn" style="margin-bottom:8px">{{ auto.blockers|join(' ') }}</div>{% endif %}
{% if auto.note %}<p class="muted" style="font-size:12px">{{ auto.note }}{% if auto.last %} ({{ auto.last[11:16] }}){% endif %}</p>{% endif %}
<form method="post" action="{{ url_for('autosend_post') }}" class="row">
<label>Per hour <input type="number" name="per_hour" min="1" max="20" value="{{ auto.cfg.per_hour }}" style="width:60px"></label>
<label>From <input type="number" name="start" min="0" max="23" value="{{ auto.cfg.start }}" style="width:56px"></label>
<label>to <input type="number" name="end" min="1" max="24" value="{{ auto.cfg.end }}" style="width:56px"></label>
{% if auto.cfg.on %}<button name="action" value="off" class="ghost">Turn off</button><button name="action" value="save" class="ghost">Save</button>
{% else %}<button name="action" value="on" {{ 'disabled' if auto.blockers }}>Turn on</button>{% endif %}
<button name="action" value="now" class="ghost" {{ 'disabled' if auto.blockers }} onclick="return confirm('Send up to {{ auto.cfg.per_hour }} prospecting emails now?')">Send a batch now</button></form>
{% if auto.next %}<details style="font-size:13px;margin-top:8px"><summary>Next up ({{ auto.next|length }})</summary><ul style="padding-left:18px;margin:6px 0">
{% for p in auto.next %}<li><b>{{ p.company }}</b> ({{ p.score }}): {{ p.name }}, {{ p.title or '' }} &lt;{{ p.email }}&gt;{% if p.email_status == 'guessed' %} <span class="muted">guessed</span>{% endif %}</li>{% endfor %}</ul></details>
{% else %}<p class="muted" style="font-size:12px">No new hiring prospects with an email address right now.</p>{% endif %}</div>
<div class="card"><h3>Replies &amp; bounces</h3>{% if imap %}<p>Your inbox is checked every 10 minutes{% if inbox_at %} (last {{ inbox_at[11:16] }}){% endif %}. Replies stop follow-ups, “unsubscribe” blocks the address, bounces try the next email guess.</p>
<form method="post" action="{{ url_for('pipeline_tools') }}"><input type="hidden" name="action" value="inbox"><button>Check now</button></form>
{% else %}<p>Mark replies with the stage menu below, or <b>connect Gmail</b> (Email box) and Zoomollo spots replies, unsubscribes and bounces in your inbox for you.</p>{% endif %}</div>
<div class="card"><h3>Loxo off-limits</h3>{% if loxo_on %}<p>{{ loxo_count }} Loxo companies{% if loxo_synced %}, synced {{ loxo_synced[:16].replace('T', ' ') }}{% else %}, not synced yet{% endif %}. Active clients and recent placements are blocked from outreach.</p>
<form method="post" action="{{ url_for('pipeline_tools') }}" class="row"><button name="action" value="loxo">Quick sync</button>
<button name="action" value="loxo_full" class="ghost" title="All companies, statuses and block flags; takes several minutes on big accounts">Full sync</button></form>
<p class="muted" style="font-size:12px;margin-top:6px">Runs by itself: jobs and placements every 6 hours, everything weekly{% if loxo_full %} (last full {{ loxo_full[:10] }}){% endif %}.</p>
{% else %}<p>Add LOXO_AGENCY_SLUG and LOXO_API_KEY to <b>zoomollo-settings.txt</b> so active clients and recent placements are blocked automatically. Until then, mark companies off-limits on their page.</p>{% endif %}</div>
<div class="card" id="phone"><h3>Phone access</h3>
{% if pin_msg %}<div class="preview" style="margin-bottom:8px">{{ pin_msg }}</div>{% endif %}
{% if zm_remote() %}<p>{{ 'On.' if pin_set else 'Off.' }} The PIN can only be changed on your PC.</p>
{% else %}<p>{% if pin_set %}<b>On.</b> Your phone opens Zoomollo through Tailscale after entering the PIN once.{% else %}<b>Off.</b> Set a PIN so your phone can open Zoomollo through Tailscale. This PC never asks for it.{% endif %}</p>
<form method="post" action="{{ url_for('phone_access') }}" class="row">
<input name="pin" type="password" inputmode="numeric" autocomplete="new-password" placeholder="{{ 'New PIN' if pin_set else 'PIN' }} (6–12 digits)" aria-label="PIN" required>
<button>{{ 'Change PIN' if pin_set else 'Turn on' }}</button></form>
{% if pin_set %}<form method="post" action="{{ url_for('phone_access') }}" style="margin-top:6px"><input type="hidden" name="action" value="off">
<button class="ghost" onclick="return confirm('Turn off phone access? Your phone is signed out.')">Turn off</button></form>{% endif %}
{% if ts_url %}<p style="font-size:13px;margin-top:8px">On your phone, open <b class="v">{{ ts_url }}</b></p>{% endif %}{% endif %}</div>
</div></details>
{% if log %}<div class="log card" style="margin-top:12px">{{ log|join('\n') }}</div>{% endif %}

{% if pipe_changes %}<div class="section"><b>Changes at your pipeline companies</b> <span class="muted">· last 7 days · <a href="{{ url_for('changes', mine=1) }}">all</a></span></div>
<div class="wrap"><table><tr><th>Company</th><th>Phone</th><th>What changed</th><th>When</th></tr>{% for e in pipe_changes[:8] %}<tr>
<td><a href="{{ url_for('company', cid=e.company_id) }}">{{ e.company_name or e.company_key }}</a></td><td>{{ phonecell(cophone(e.company_id)) }}</td><td class="wraptext">{{ e.detail }}</td><td>{{ e.created_at[:10] }}</td></tr>{% endfor %}</table></div>{% endif %}
{% if best %}<div class="section"><b>Best accounts to work next</b> <span class="muted">· highest hiring score, not in your pipeline</span></div>
<div class="wrap"><table><tr><th>Score</th><th>Company</th><th>Phone</th><th>Why</th><th></th></tr>
{% for sc, c in best %}<tr><td>{{ scorechip(sc) }}</td><td><a href="{{ url_for('company', cid=c.id) }}"><b>{{ c.name or c.key }}</b></a></td><td>{{ phonecell(cophone(c.id)) }}</td>
<td class="wraptext why">{{ sc.top_reasons|join(' · ') }}</td>
<td><form class="fb" method="post" action="{{ url_for('pipeline_post', cid=c.id) }}"><input type="hidden" name="action" value="add"><button>+ Pipeline</button></form></td></tr>{% endfor %}</table></div>{% endif %}

<div class="section"><b>Due today</b> <span class="muted">· {{ due|length }}</span></div>
<div class="wrap"><table><tr><th>Who</th><th>What</th><th></th></tr>
{% for it in due %}{% if it.kind == 'task' %}<tr><td><a href="{{ url_for('company', cid=it.company_id) }}">{{ it.company_name }}</a><br>{{ phonecell(cophone(it.company_id)) }}</td>
<td class="wraptext"><span class="reason">next step{% if it.due < today %} · <span class="overdue">overdue</span>{% endif %}</span><br>{{ it.next_step }} <span class="muted">(due {{ it.due }})</span></td>
<td><form class="fb" method="post" action="{{ url_for('pipeline_post', cid=it.company_id) }}"><input type="hidden" name="action" value="task_done"><button>Done</button></form></td></tr>
{% else %}<tr><td><b>{{ it.name }}</b><br><span class="muted">{{ it.title or '' }}</span><br><a href="{{ url_for('company', cid=it.company_id) }}">{{ it.company_name }}</a><br>{{ phonecell(it.phone, cophone(it.company_id)) }}</td>
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
<br><span style="font-size:13px">{{ phonecell(cophone(c.company_id)) }}</span>
{% if c.next_step %}<br><span class="{{ 'overdue' if c.overdue }}">{{ c.next_step }}{% if c.due_date %} · {{ c.due_date }}{% endif %}</span>{% endif %}
<form method="post" action="{{ url_for('pipeline_post', cid=c.company_id) }}"><input type="hidden" name="action" value="stage">
<select name="stage" onchange="this.form.submit()">{% for k2, l2 in stages %}<option value="{{ k2 }}" {{ 'selected' if k2 == k }}>{{ l2 }}</option>{% endfor %}</select></form></div>
{% else %}<p class="muted" style="font-size:13px">—</p>{% endfor %}</div>{% endif %}{% endfor %}</div>"""

OPENED = """<div class="card" style="margin-bottom:14px"><p style="margin:0">People who opened an email you sent, from the
"opened" notifications and read receipts in your inbox (Gmail label <b>{{ folder }}</b> too). Hottest first: call them while it's fresh.
{% if checked %}Inbox last checked {{ checked[:16].replace('T', ' ') }}.{% else %}<b>Your inbox hasn't been checked yet</b>: set up email under Messages.{% endif %}</p></div>
{% if not rows %}<p class="muted">No opens yet. They appear here as the notifications arrive in Gmail.</p>{% else %}
<div class="wrap"><table><tr><th>Who</th><th>Company</th><th>Opened</th><th>Times</th><th>Last opened</th></tr>
{% for r in rows %}<tr><td><b>{{ r.name or r.email }}</b>{% if r.title %}<br><span class="muted">{{ r.title }}</span>{% endif %}
{% if r.email %}<br><a href="mailto:{{ r.email }}">{{ r.email }}</a>{% endif %}{% if r.candidate_id %} <span class="chip">candidate</span>{% endif %}</td>
<td>{% if r.company_id %}<a href="{{ url_for('company', cid=r.company_id) }}">{{ r.company_name or '' }}</a>{% else %}{{ r.cand_company or '' }}{% endif %}</td>
<td class="wraptext">{{ r.subject or '' }}</td><td>{{ r.times }}</td><td>{{ r.last[:16].replace('T', ' ') }}</td></tr>{% endfor %}
</table></div>{% endif %}"""

CHANGES = SCORE_MACRO + PHONE_MACRO + """<form class="bar">
<select name="days" onchange="this.form.submit()">{% for d in [7, 14, 30, 90] %}<option value="{{ d }}" {{ 'selected' if d == days }}>Last {{ d }} days</option>{% endfor %}</select>
<label class="chip" style="padding:8px 10px"><input type="checkbox" name="niche" value="1" {{ 'checked' if niche }} style="padding:0" onchange="this.form.submit()"> Only my niche</label>
<label class="chip" style="padding:8px 10px"><input type="checkbox" name="mine" value="1" {{ 'checked' if mine }} style="padding:0" onchange="this.form.submit()"> Only my pipeline</label></form>
<div class="card" style="margin-bottom:14px"><p style="margin:0">Companies are re-checked every {{ every }} days while Zoomollo is open (up to {{ per_hour }} an hour). {{ due_count }} due now{% if last_run %}; last re-check {{ last_run[:16].replace('T', ' ') }}{% endif %}.</p>
<form method="post" action="{{ url_for('recheck_now') }}" style="margin-top:8px"><button {{ 'disabled' if not due_count }}>Re-check {{ [due_count, per_hour]|min }} now</button></form></div>
{% for kind, label in kinds.items() %}<div class="section"><b>{{ label }}</b> <span class="muted">· {{ groups[kind]|length }}</span>
{% if kind == 'job_reposted' %}<span class="muted"> · strongest signal: they couldn't fill it</span>{% elif kind == 'job_gone' %}<span class="muted"> · ask who filled it, or if it's on hold</span>{% elif kind == 'person_new' %}<span class="muted"> · new managers often build their team</span>{% endif %}</div>
<div class="wrap"><table><tr><th>Score</th><th>Company</th><th>Phone</th><th>What changed</th><th>When</th><th></th></tr>
{% for e in groups[kind] %}<tr><td>{% if e.score %}{{ scorechip(e.score) }}{% endif %}</td>
<td><a href="{{ url_for('company', cid=e.company_id) }}"><b>{{ e.company_name or e.company_key }}</b></a>{% if e.city %}<br><span class="muted">{{ e.city }}</span>{% endif %}</td>
<td>{{ phonecell(cophone(e.company_id)) }}</td>
<td class="wraptext">{% if e.job_url %}<a href="{{ e.job_url }}" target="_blank" rel="noopener">{{ e.detail }}</a>{% else %}{{ e.detail }}{% endif %}{% if e.fit %} <span class="chip fit">fits my desk</span>{% endif %}</td>
<td>{{ e.created_at[:10] }}</td>
<td>{% if e.stage %}<span class="chip">{{ stage_names[e.stage] }}</span>{% elif e.score and not e.score.offlimits.blocked %}<form class="fb" method="post" action="{{ url_for('pipeline_post', cid=e.company_id) }}"><input type="hidden" name="action" value="add"><button class="ghost">+ Pipeline</button></form>{% endif %}</td></tr>
{% else %}<tr><td colspan="6" class="muted">None.</td></tr>{% endfor %}</table></div>{% endfor %}"""

KCHIPS = """{% macro kchips(groups, m, jump=False) %}{% for g in groups %}{% set n = m.hits[loop.index0] if m and m.hits else 0 %}
<span class="kchip {{ 'k' ~ g.color if n else 'miss' }}" {% if jump and n %}onclick="zmNext({{ g.color }})" title="Jump to next match"{% endif %}>{{ g.label }}{% if n %}<span class="n">{{ n }}</span>{% endif %}</span>{% endfor %}{% endmacro %}"""

CANDIDATES = KCHIPS + PHONE_MACRO + """<form class="bar"><input type="search" name="q" value="{{ q }}" placeholder="e.g. tax manager with experience reviewing who lives in Texas or Wisconsin" style="flex:3">
<select name="source"><option value="">All candidates</option><option value="loxo" {{ 'selected' if src == 'loxo' }}>From Loxo</option><option value="upload" {{ 'selected' if src == 'upload' }}>Uploaded</option></select><button>Search</button></form>
{% if groups %}<div class="card" style="margin-bottom:12px"><div>{% for g in groups %}<span class="kchip k{{ g.color }}">{{ g.label }}</span>{% endfor %}</div>
<div class="muted" style="font-size:12px;margin-top:6px">Searching for: <span class="v">{{ describe }}</span>. Edit it above for exact control: commas separate keyword groups, "|" separates alternatives. Words match their forms (review ~ reviewed, reviewer); states match their abbreviation.</div></div>{% endif %}
<details class="zm-fold" open><summary>Candidate totals, Loxo sync and résumé upload</summary><div class="grid" style="margin-bottom:12px">
<div class="card"><h3>Candidates</h3><p>{{ counts.total }} candidates ({{ counts.loxo }} from Loxo, {{ counts.uploaded }} uploaded), {{ counts.with_resume }} with a résumé.{% if counts.backlog %} {{ counts.backlog }} Loxo résumés still to download (they come in the background; search results first).{% endif %}</p>
{% if loxo_on %}<form method="post" action="{{ url_for('candidates_tools') }}" class="row"><button name="action" value="sync">Sync candidates from Loxo</button>
{% if counts.backlog %}<button name="action" value="resumes" class="ghost">Download 50 résumés now</button>{% endif %}</form>{% if synced %}<p class="muted" style="font-size:12px;margin-top:6px">Last synced {{ synced[:16].replace('T', ' ') }}.</p>{% endif %}{% endif %}</div>
<form class="card" method="post" action="{{ url_for('candidates_upload') }}" enctype="multipart/form-data"><h3>Upload résumés</h3>
<p>PDF, Word (.docx) or text. Read on this computer; name, email, phone and location are picked up automatically.</p>
<input type="file" name="files" multiple accept=".pdf,.docx,.txt,.rtf" required style="width:100%;margin-bottom:8px"><button>Upload</button>
{% if upload_msg %}<p class="muted" style="margin-top:8px">{{ upload_msg }}</p>{% endif %}</form></div></details>
{% if q %}<div class="section"><b>{{ results|length }} candidate{{ '' if results|length == 1 else 's' }}</b> <span class="muted">· best matches first</span></div>{% endif %}
{% for r in results %}{% set c = r.c %}<div class="card cand" style="margin-bottom:10px">
<div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap"><div><a href="{{ url_for('candidate', cid=c.id, q=q) }}"><b>{{ c.name or 'Unnamed' }}</b></a>
{% if r.fit %} <span class="fitlbl">· {{ r.fit }}</span>{% endif %}<br><span class="muted">{{ c.title or '' }}{% if c.company %} · {{ c.company }}{% endif %}{% if c.city or c.state %} · {{ [c.city, c.state]|select|join(', ') }}{% endif %}</span>
<br><span style="font-size:13px">📞 {{ phonecell(c.phone) }}</span></div>
<div class="muted" style="font-size:12px;text-align:right">{{ 'Loxo' if c.source == 'loxo' else 'Uploaded' }}{% if not c.resume_text %}<br>{{ 'résumé downloading…' if c.source == 'loxo' and not c.resume_status else 'no résumé' }}{% endif %}</div></div>
{% if groups %}<div style="margin-top:6px">{{ kchips(groups, r.match) }}</div>{% for sn in r.snips %}<div class="snip">{{ sn|safe }}</div>{% endfor %}{% endif %}</div>
{% else %}{% if q %}<p class="muted">No candidates match any of these keywords.</p>{% elif not counts.total %}<p class="muted">No candidates yet: sync from Loxo or upload résumés.</p>{% endif %}{% endfor %}
{% if not q and results %}<p class="muted">Type a search above to rank candidates and see their keywords.</p>{% endif %}"""

CANDIDATE = KCHIPS + OL_MACRO + SCORE_MACRO + PHONE_MACRO + """<h2>{{ c.name or 'Unnamed' }}</h2>
<div class="sub">{{ c.title or '' }}{% if c.company %} · {{ c.company }}{% endif %}{% if c.city or c.state %} · {{ [c.city, c.state]|select|join(', ') }}{% endif %}
{% if c.email %} · <a href="mailto:{{ c.email }}">{{ c.email }}</a>{% endif %}{% if c.phone %} · <a href="tel:{{ c.phone }}">{{ c.phone|phone }}</a>{% else %} · <span class="muted">phone not found</span>{% endif %}{% if c.linkedin_url %} · <a href="{{ c.linkedin_url }}" target="_blank" rel="noopener">LinkedIn</a>{% endif %}
· {{ 'from Loxo' if c.source == 'loxo' else 'uploaded' }}{% if years %} · about {{ years }} years' experience{% endif %}</div>
<form class="bar" action="{{ url_for('candidate', cid=c.id) }}"><input type="search" name="q" value="{{ q }}" placeholder="View this résumé through keywords, e.g. tax manager, review, Texas | Wisconsin"><button>Highlight</button></form>
{% if groups %}<div style="margin-bottom:10px">{{ kchips(groups, m, True) }} <span class="muted" style="font-size:12px">click a keyword to jump to its next match</span></div>{% endif %}
<div class="grid"><div class="card big" style="padding:0;border:0">
{% if resume %}<div class="resume" id="resume">{{ resume|safe }}</div>
{% elif c.source == 'loxo' %}<div class="card">Résumé not downloaded yet{% if c.resume_status and c.resume_status != 'queued' %} ({{ c.resume_status }}){% endif %}.
<form method="post" action="{{ url_for('candidate_fetch', cid=c.id) }}" style="margin-top:8px"><button>Download résumé now</button></form>
{% if c.skills %}<div class="resume" style="margin-top:10px">{{ skills|safe }}</div>{% endif %}</div>
{% else %}<div class="card">No résumé text.</div>{% endif %}</div></div>

<div class="section"><b>Companies hiring for this</b> <span class="muted">· open roles matching “{{ c.title or '—' }}”, best hiring score first</span></div>
<div class="wrap"><table><tr><th>Score</th><th>Role</th><th>Company</th><th>Hiring manager</th><th>Phone</th><th></th></tr>
{% for r in jobs %}<tr><td>{% if r.score %}{{ scorechip(r.score) }}{% endif %}</td>
<td>{% if r.job.url %}<a href="{{ r.job.url }}" target="_blank" rel="noopener">{{ r.job.title }}</a>{% else %}{{ r.job.title }}{% endif %}</td>
<td><a href="{{ url_for('company', cid=r.job.company_id) }}">{{ r.job.company_name or r.job.company_key }}</a> {{ olbadge(r.offlimits) }}</td>
<td>{% if r.manager %}{{ r.manager.name }}<br><span class="muted">{{ r.manager.title or '' }}{% if r.manager.email %} · {{ r.manager.email }}{% endif %}</span>{% else %}<span class="muted">not found yet</span>{% endif %}</td>
<td>{{ phonecell(r.manager.phone if r.manager else None, cophone(r.job.company_id)) }}</td>
<td>{% if not r.offlimits.blocked %}<a class="btn" href="{{ url_for('pitch_page', candidate=c.id, job=r.job.id, q=q) }}">Pitch</a>{% endif %}</td></tr>
{% else %}<tr><td colspan="6" class="muted">{% if c.title %}No open roles like this yet. Prospect for companies hiring "{{ c.title }}".{% else %}Add the candidate's title to find matching roles.{% endif %}</td></tr>{% endfor %}</table></div>
<script>
var zmPos = {};
function zmNext(k) {
  var marks = document.querySelectorAll("#resume mark.k" + k); if (!marks.length) return;
  zmPos[k] = ((zmPos[k] === undefined ? -1 : zmPos[k]) + 1) % marks.length;
  var el = marks[zmPos[k]]; el.scrollIntoView({block: "center", behavior: "smooth"});
  el.style.outline = "3px solid #111"; setTimeout(function () { el.style.outline = ""; }, 900);
}
</script>"""

PITCH = """<h2>Pitch {{ c.title or 'candidate' }} to {{ company }}</h2><div class="sub">{{ job.title }}{% if p.person %} · to {{ p.person.name }}{% if p.person.title %}, {{ p.person.title }}{% endif %}{% endif %}
· candidate details are anonymised (no name or current employer)</div>
{% if offl.level %}<div class="warn">{{ offl.reason }}</div>{% endif %}
{% if not p.to %}<div class="warn">No email for this role's hiring manager yet. Find one on the <a href="{{ url_for('company', cid=job.company_id) }}">company page</a>, or copy the text below.</div>{% endif %}
<form method="post" action="{{ url_for('pitch_send') }}" class="card tpl">
<input type="hidden" name="candidate" value="{{ c.id }}"><input type="hidden" name="job" value="{{ job.id }}">
<input name="to" value="{{ p.to or '' }}" placeholder="To" style="width:100%;margin-bottom:6px">
<input name="subject" value="{{ p.subject }}" style="width:100%;margin-bottom:6px"><textarea name="body" style="min-height:260px">{{ p.body }}</textarea>
{% if p.missing %}<p class="muted">Filled generically: {{ p.missing|join(', ') }}.</p>{% endif %}
{% if offl.blocked %}<label class="chip" style="padding:8px 10px"><input type="checkbox" name="override" value="1" required style="padding:0"> Send anyway</label>{% endif %}
<div class="row" style="margin-top:8px">{% if smtp %}<button name="action" value="smtp">Send</button>{% endif %}<button name="action" value="mailto" class="{{ 'ghost' if smtp }}">Open in email app</button>
<a class="btn" href="{{ url_for('candidate', cid=c.id, q=q) }}">Back</a></div></form>"""

JOBS = PHONE_MACRO + """<form class="bar"><input type="search" name="q" value="{{ q }}" placeholder="Role, company, city, contact…">
<label class="chip" style="padding:8px 10px"><input type="checkbox" name="niche" value="1" {{ 'checked' if niche }} style="padding:0"> Only my niche</label>
{% if area %}<label class="chip" style="padding:8px 10px" title="Your area: {{ area }}"><input type="checkbox" name="anywhere" value="1" {{ 'checked' if anywhere }} style="padding:0"> Show roles outside my area{% if hidden %} ({{ hidden }} hidden){% endif %}</label>{% endif %}<button>Search</button>
<a class="btn" href="{{ url_for('export_hiring', q=q) }}">Export roles × contacts CSV</a></form>
<div class="count">{{ count_line }}</div>
<div class="wrap"><table><tr><th>Company</th><th>Phone</th><th>Role</th><th>Location</th><th>Source</th><th>Best contacts</th></tr>
{% for j in rows %}<tr><td><a href="{{ url_for('company', cid=j.company_id) }}">{{ j.company_name or j.company_key }}</a></td>
<td>{{ phonecell(cophone(j.company_id)) }}</td>
<td>{% if j.url %}<a href="{{ j.url }}" target="_blank" rel="noopener">{{ j.title }}</a>{% else %}{{ j.title }}{% endif %}
{% if fits[j.id][0] %} <span class="chip fit">fits my desk</span>{% elif desk_set %} <span class="muted" style="font-size:12px">{{ fits[j.id][1] }}</span>{% endif %}
{% if fits[j.id][0] %}{% for m in pitch_for(j, 2) %}<br><span style="font-size:12px">👤 <a href="{{ url_for('candidate', cid=m.c.id) }}">{{ m.c.name }}</a> <span class="muted">{{ m.c.title }}{% if m.c.city %}, {{ m.c.city }}{% endif %}</span> · <a href="{{ url_for('pitch_page', candidate=m.c.id, job=j.id) }}">pitch</a></span>{% endfor %}{% endif %}
<br><a style="font-size:12px" href="{{ url_for('candidates', q=j.title ~ ((', ' ~ j.location) if j.location else '')) }}">Find candidates</a></td>
<td>{% if j.location %}{{ j.location }}{% else %}<span class="muted" title="The post doesn't say where; counted as local only because of the company's address">location unknown</span>{% endif %}</td><td><span class="chip">{{ j.board }}</span></td>
<td class="wraptext">{% for m in matches.get(j.id, [])[:3] %}<div><span class="reason">{{ m.reason }}</span> {{ m.name or '' }}
<span class="v">{{ m.email or m.value or '' }}</span></div>{% else %}<span class="muted">none yet</span>{% endfor %}</td></tr>
{% else %}<tr><td colspan="6">No jobs yet.</td></tr>{% endfor %}</table></div>"""


BULK_CAP = 50  # most people one click can enrol in follow-ups


def showing(shown: int, total: int, noun: str, filtered: bool = False, cap: int | None = None,
            extra: str = "") -> str:
    """'Showing all 881 people' / 'Showing 12 matching of 881 people' / 'Showing the first 500 of …'."""
    tail = f" · {extra}" if extra else ""
    if cap and shown >= cap:
        return f"Showing the first {shown:,} {'matching ' if filtered else ''}of {total:,} {noun}. Search to narrow it down.{tail}"
    if filtered:
        return f"Showing {shown:,} matching of {total:,} {noun}{tail}"
    if shown == total:
        return f"Showing all {total:,} {noun}{tail}"
    return f"Showing {shown:,} of {total:,} {noun}{tail}"


PHONE_ROWS = 60  # a longer list is slow to scroll and to draw on a phone


def phone_trim(rows, line: str):
    """On a phone, the first PHONE_ROWS rows and a line that says so."""
    ua = request.headers.get("User-Agent", "")
    if ("Mobi" in ua or "Android" in ua or "iPhone" in ua) and len(rows) > PHONE_ROWS:
        return rows[:PHONE_ROWS], f"{line} · first {PHONE_ROWS} shown on your phone: search to narrow it down"
    return rows, line


def nice_phone(value) -> str:
    """+12145551234 -> (214) 555-1234; other countries keep their + code."""
    v = str(value or "")
    d = re.sub(r"\D", "", v)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) == 10 and (not v.startswith("+") or v.startswith("+1")):
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return v


PIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Zoomollo</title>
<style>:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e4e4df;--accent:#2f5bea;--bad:#991b1b;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ececE8;--muted:#9a9a94;--line:#2e2e2b;--accent:#7d9bff;--bad:#fca5a5;color-scheme:dark}}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px;box-sizing:border-box}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px;width:100%;max-width:340px}
input,button{font:inherit;font-size:20px;width:100%;box-sizing:border-box;padding:12px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--ink)}
input{letter-spacing:.3em;text-align:center;margin:12px 0}button{background:var(--accent);color:#fff;border:none}
p{color:var(--muted);margin:6px 0}.bad{color:var(--bad)}</style></head><body><div class="card">
<h2 style="margin:0 0 4px">Zoomollo</h2>
{% if not pin_set %}<p>Phone access is off. On your PC, open <b>Pipeline</b> and set a PIN under <b>Phone access</b>.</p>
{% elif locked %}<p class="bad">Too many wrong PINs. Try again in {{ locked }} minute{{ 's' if locked != 1 }}.</p>
{% else %}<p>Enter your PIN.</p>{% if wrong %}<p class="bad">That PIN didn't match.</p>{% endif %}
<form method="post" action="{{ url_for('pin_check') }}"><input type="hidden" name="next" value="{{ next }}">
<input name="pin" type="password" inputmode="numeric" autocomplete="current-password" aria-label="PIN" autofocus required>
<button>Unlock</button></form>{% endif %}</div></body></html>"""


def _install_phone_access(app: Flask, db_path: str | None) -> None:
    """Your PC opens Zoomollo with no PIN. Anything else (your phone over Tailscale) must enter the PIN once;
    the phone then stays unlocked for 90 days, or until the PIN is changed."""
    import secrets
    import time
    from datetime import timedelta
    from flask import session

    db = DB(db_path)
    key = db.get_setting("web_secret")
    if not key:
        key = secrets.token_hex(32)
        db.set_setting("web_secret", key)
        db.commit()
    app.secret_key = key
    app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
                      PERMANENT_SESSION_LIFETIME=timedelta(days=90))
    fails: dict = {"n": 0, "until": 0.0}

    def is_remote() -> bool:
        # Tailscale Serve hands requests over from this PC, so also look for the headers it adds.
        if request.remote_addr not in ("127.0.0.1", "::1"):
            return True
        return any(request.headers.get(h) for h in ("X-Forwarded-For", "X-Forwarded-Host", "Forwarded",
                                                    "Tailscale-User-Login"))
    app.jinja_env.globals["zm_remote"] = is_remote

    def pin_ver(d: DB) -> str:
        return (d.get_setting("remote_pin_hash") or "")[-12:]

    @app.before_request
    def _gate():
        if not is_remote() or request.endpoint in ("pin_check", "pin_page"):
            return None
        d = DB(db_path)
        ver = pin_ver(d)
        if ver and session.get("pin_ok") == ver:
            return None
        if request.method == "GET":
            return redirect(url_for("pin_page", next=request.full_path.rstrip("?")))
        abort(403)

    @app.get("/pin")
    def pin_page():
        d = DB(db_path)
        left = max(0, int((fails["until"] - time.time() + 59) // 60))
        return render_template_string(PIN_PAGE, pin_set=bool(pin_ver(d)), locked=left,
                                      wrong=request.args.get("wrong") == "1", next=request.args.get("next", "/"))

    @app.post("/pin")
    def pin_check():
        from werkzeug.security import check_password_hash
        d = DB(db_path)
        nxt = request.form.get("next", "/")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        h = d.get_setting("remote_pin_hash")
        if not h or time.time() < fails["until"]:
            return redirect(url_for("pin_page", next=nxt))
        if check_password_hash(h, "".join(request.form.get("pin", "").split())):
            fails["n"] = 0
            session.permanent = True
            session["pin_ok"] = pin_ver(d)
            return redirect(nxt)
        fails["n"] += 1
        if fails["n"] >= 5:  # 5 wrong in a row: locked for 15 minutes
            fails["n"], fails["until"] = 0, time.time() + 15 * 60
        return redirect(url_for("pin_page", next=nxt, wrong=1))

    @app.post("/phone-access")
    def phone_access():
        from werkzeug.security import generate_password_hash
        if is_remote():
            abort(403)  # the PIN can only be set or removed on the PC itself
        d = DB(db_path)
        f = request.form
        if f.get("action") == "off":
            d.set_setting("remote_pin_hash", "")
            msg = "Phone access is off. Phones are signed out."
        else:
            pin = "".join(f.get("pin", "").split())
            if not (pin.isdigit() and 6 <= len(pin) <= 12):
                return redirect(url_for("pipeline_page", pin_msg="Use 6 to 12 digits.", _anchor="phone"))
            d.set_setting("remote_pin_hash", generate_password_hash(pin))
            msg = "PIN saved. Any phone signed in before has to enter the new PIN."
        d.commit()
        return redirect(url_for("pipeline_page", pin_msg=msg, _anchor="phone"))


def _local_hour() -> int:
    """The hour on this computer's clock (texting quiet hours). Tests replace it."""
    from datetime import datetime
    return datetime.now().hour


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.jinja_env.filters["phone"] = nice_phone
    from .today import line_note
    app.jinja_env.filters["linenote"] = line_note
    _install_phone_access(app, db_path)

    import time as _boot_t
    started_label = _boot_t.strftime("%b %d, %I:%M %p").replace(" 0", " ")

    @app.after_request
    def shrink(resp):
        """Send pages compressed: a phone downloads a fifth of the bytes."""
        import gzip
        if (resp.mimetype or "").startswith("text/html"):
            # Never let a phone show a saved copy: every page is fetched fresh, so an update shows at once.
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
        if ("gzip" not in request.headers.get("Accept-Encoding", "") or resp.direct_passthrough
                or resp.status_code != 200 or "Content-Encoding" in resp.headers
                or not (resp.mimetype or "").startswith(("text/", "application/json"))):
            return resp
        data = resp.get_data()
        if len(data) < 2000:
            return resp
        resp.set_data(gzip.compress(data, 5))
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Vary"] = "Accept-Encoding"
        return resp

    tasks: "queue.Queue[tuple[str, str, dict]]" = queue.Queue()
    log: deque[str] = deque(maxlen=300)
    state = {"busy": 0}
    notices: deque = deque(maxlen=5)  # "search finished" messages shown at the top of every page

    def notify(text: str, tab: str | None = None) -> None:
        import time as _t
        notices.appendleft({"text": text, "tab": tab, "at": _t.time()})

    from . import workqueue

    def submit(action: str, item: str, opts: dict | None = None) -> bool:
        """Queue work. Crawls and imports are saved in the database first, so a restart
        continues them, and the same one is not queued twice."""
        opts = dict(opts or {})
        if action in workqueue.DURABLE and "_task_id" not in opts:
            task_id = workqueue.enqueue(DB(db_path), action, item, opts)
            if not task_id:
                log.appendleft(f"already queued: {action} {item}")
                return False
            opts["_task_id"] = task_id
        tasks.put((action, item, opts))
        return True

    def worker():
        db = DB(db_path)  # own connection for the background thread
        while True:
            action, item, opts = tasks.get()
            state["busy"] += 1
            task_id = opts.get("_task_id")
            try:
                if task_id and not opts.get("_claimed"):
                    if not workqueue.claim(db, task_id):
                        task_id = None
                        continue  # a restart already handed this one to another pass
                    opts = {**opts, "_claimed": True}
                if action == "inbox":
                    from . import crm
                    r = crm.check_inbox(db)
                    if r["reply"] or r["bounce"] or r["unsubscribe"] or r["opened"] or r["sent"] or opts.get("loud"):
                        log.appendleft(f"✓ Inbox: {r['reply']} replies, {r['opened']} opens, {r['sent']} sent from "
                                       f"Outlook, {r['unsubscribe']} unsubscribes, {r['bounce']} bounces")
                    if r["opened"]:
                        notify(f"{r['opened']} people opened your email. See Opened my email.", "opened")
                elif action == "autosend":
                    from . import autosend
                    r = autosend.run_hour(db, progress=log.appendleft, force=opts.get("force", False))
                    if r["sent"]:
                        log.appendleft(f"✓ Sent {r['sent']} prospecting emails ({r['new']} new, "
                                       f"{r['followups']} follow-ups)")
                        notify(f"Auto-send: {r['sent']} prospecting emails went out.", "pipeline_page")
                    elif r["note"] and (opts.get("force") or "paused" in r["note"]):
                        notify(r["note"], "pipeline_page")
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
                    notify(f"Search finished: {opts['what']} in {opts['where']} added {r['companies']} "
                           f"compan{'y' if r['companies'] == 1 else 'ies'} and {r['jobs']} jobs.", "companies")
                elif action == "directory":
                    log.appendleft(f"reading directory {item}…")
                    r = pipeline.crawl_directory(db, item, opts.get("what", ""), opts["where"],
                                                 limit=opts.get("limit", 40), progress=log.appendleft)
                    log.appendleft(f"✓ {item}: {r['companies']} companies kept of {r['checked']} checked, "
                                   f"{r['people']} people, {r['jobs']} jobs")
                    notify(f"Directory finished: {r['companies']} companies added from {item}.", "companies")
                elif action in ("crawl", "addco"):
                    log.appendleft(f"crawling {item}…")
                    if action == "addco":  # a company you know: saved under your vertical and city first
                        from .crawler import normalize_domain
                        dom = normalize_domain(item)
                        cid = db.company(dom, domain=dom)
                        db.update_company(cid, industry=opts.get("what") or None, city=opts.get("where") or None,
                                          source="added by hand")
                        db.commit()
                    r = pipeline.crawl_company(db, item, max_pages=40)
                    if r.get("blocked"):
                        log.appendleft(f"✗ {item}: skipped, {r['blocked']}")
                    elif not r["pages"]:
                        extra = " Gave up after the time limit and backed off." if r.get("gave_up") else ""
                        log.appendleft(f"✗ {item}: site didn't load, nothing saved from it. Check the address.{extra}")
                    else:
                        log.appendleft(f"✓ {item}: {r['pages']} pages, {r['emails']} emails, {r['people']} people")
                    if action == "addco":
                        notify(f"{item} added: {r['people']} people, {r['jobs']} jobs" if r["pages"] else
                               f"{item} couldn't be read. Check the website address.", "companies")
                elif action == "jobs":
                    refs = [item] if ":" in item and "://" not in item else None
                    if "://" in item or "myworkdayjobs.com" in item:  # a pasted careers link (any job system)
                        from .extract import find_ats
                        refs = [f"{b}:{s}" for b, s in find_ats(item)] or None
                    if refs is None:
                        from .jobboards import detect_board
                        refs = [f"{b}:{item}" for b, _ in detect_board(item)]
                        if not refs:
                            log.appendleft(f"✗ {item}: no public job board found")
                    for ref in refs:
                        board, _, slug = ref.partition(":")
                        r = pipeline.import_jobs(db, board, slug)
                        log.appendleft(f"✓ {ref}: {r['jobs']} jobs, {r['emails']} emails")
                if task_id:
                    workqueue.finish(db, task_id, "done")
            except Exception as e:  # noqa: BLE001
                try:
                    db.conn.rollback()  # never leave a half-finished write holding the database
                except Exception:  # noqa: BLE001
                    pass
                tries = opts.get("_tries", 0)
                if "database is locked" in str(e) and tries < 3:
                    # Another task (e.g. a big Loxo sync) was writing; try again shortly.
                    new_opts = {**opts, "_tries": tries + 1, "_claimed": True}
                    if task_id:
                        workqueue.keep(db, task_id, new_opts)
                    log.appendleft(f"… {item}: database busy, retrying in 30s")
                    threading.Timer(30, tasks.put, args=((action, item, new_opts),)).start()
                else:
                    if task_id:
                        workqueue.finish(db, task_id, "failed")
                    if action in ("crawl", "addco"):
                        try:
                            from .crawler import normalize_domain as _norm
                            from .pipeline import note_crawl_result
                            row = db.conn.execute(
                                "SELECT id FROM companies WHERE key = ?", (_norm(str(item)),)).fetchone()
                            if row:
                                note_crawl_result(db, row["id"], False)
                        except Exception:  # noqa: BLE001
                            pass
                    log.appendleft(f"✗ {item}: {e}")
            finally:
                state["busy"] -= 1
                tasks.task_done()

    for queued in workqueue.recover(DB(db_path)):
        tasks.put(queued)  # tasks left waiting, or running when the process last stopped
    threading.Thread(target=worker, daemon=True).start()

    def warm_up():
        """Right after a start, do the slow first-time work in the background so your first tap isn't the one that waits."""
        import time
        time.sleep(2)
        try:
            from . import candidates as cands
            with app.test_client() as c:
                for path in ("/", "/jobs", "/pipeline", "/companies"):
                    c.get(path)
            cands._documents(DB(db_path), None)
        except Exception as e:  # noqa: BLE001 - a warm-up must never stop the app
            log.appendleft(f"warm-up skipped: {e}")
        try:  # accounts on your desk with no named leader yet: look them up in Apollo (free search)
            from . import leads as _leads
            _leads.backfill(DB(db_path), limit=60, progress=log.appendleft)
            _leads.auto_enrich(DB(db_path), progress=log.appendleft)
        except Exception as e:  # noqa: BLE001
            log.appendleft(f"Apollo leader lookup skipped: {e}")

    if os.environ.get("ZOOMOLLO_NO_BACKGROUND") != "1":
        threading.Thread(target=warm_up, daemon=True).start()

    def inbox_timer():
        import time
        from . import crm
        while True:
            time.sleep(600)
            if crm.imap_configured():
                tasks.put(("inbox", "inbox", {}))

    def autosend_timer():
        """Once per clock hour, inside your sending hours: queue the auto-send run."""
        import time
        from datetime import datetime
        from . import autosend
        db = DB(db_path)
        time.sleep(90)  # let the first inbox check catch bounces and replies first
        while True:
            try:
                cfg = autosend.settings(db)
                hour = datetime.now().strftime("%Y-%m-%dT%H")
                if cfg["on"] and autosend.in_window(cfg) and db.get_setting("autosend_hour") != hour:
                    db.set_setting("autosend_hour", hour)
                    tasks.put(("autosend", "auto-send", {}))
            except Exception as e:  # noqa: BLE001
                log.appendleft(f"✗ auto-send timer: {e}")
            time.sleep(120)

    if os.environ.get("ZOOMOLLO_NO_BACKGROUND") != "1":
        threading.Thread(target=inbox_timer, daemon=True).start()
        threading.Thread(target=autosend_timer, daemon=True).start()

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

    def loxo_activity_timer():
        """Every minute: send logged calls, emails, texts and pitches to Loxo (after the Undo window)."""
        import time
        from . import loxosync
        time.sleep(20)
        while True:
            try:
                loxosync.flush(DB(db_path), progress=log.appendleft)
            except Exception as e:  # noqa: BLE001 - keep trying next minute
                log.appendleft(f"✗ Loxo activity sync: {e}")
            time.sleep(60)

    if os.environ.get("ZOOMOLLO_NO_BACKGROUND") != "1":
        threading.Thread(target=loxo_activity_timer, daemon=True).start()

    recheck_busy = threading.Lock()

    def run_recheck():
        """Re-check companies that are due (own thread: each site takes a few seconds)."""
        from . import recheck
        if not recheck_busy.acquire(blocking=False):
            return
        try:
            db = DB(db_path)
            pipeline.fill_missing_guesses(db, progress=log.appendleft)
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

    cand_busy = threading.Lock()

    def run_candidate_sync():
        from . import candidates as cands
        if not cand_busy.acquire(blocking=False):
            log.appendleft("Candidate sync already running")
            return
        try:
            r = cands.sync_loxo(DB(db_path), progress=log.appendleft)
            log.appendleft(f"✓ Candidates synced: {r['candidates']} candidates ({r['people']} Loxo people read)")
        except Exception as e:  # noqa: BLE001
            log.appendleft(f"✗ Candidate sync failed: {e}")
        finally:
            cand_busy.release()

    resume_busy = threading.Lock()

    def run_resume_fill(n: int):
        from . import candidates as cands
        if not resume_busy.acquire(blocking=False):
            return
        try:
            cands.fill_resumes(DB(db_path), limit=n, progress=log.appendleft)
        except Exception as e:  # noqa: BLE001
            log.appendleft(f"✗ Résumé download failed: {e}")
        finally:
            resume_busy.release()

    def candidate_timer():
        """Weekly candidate sync; résumés download a few at a time (search results first)."""
        import time
        from datetime import datetime, timedelta, timezone
        from . import loxo
        time.sleep(20)
        while True:
            if loxo.configured():
                db = DB(db_path)
                last = db.get_setting("candidates_synced_at")
                try:
                    stale = not last or datetime.fromisoformat(last) < datetime.now(timezone.utc) - timedelta(days=7)
                except ValueError:
                    stale = True
                if stale:
                    run_candidate_sync()
                run_resume_fill(25)
            time.sleep(300)

    if os.environ.get("ZOOMOLLO_NO_BACKGROUND") != "1":
        threading.Thread(target=candidate_timer, daemon=True).start()

    def loxo_contact_timer():
        """Every half hour: read back contact info Loxo found, and add prospects who are missing details.

        No daily cap on Loxo adds. Rate limits are handled on the API calls (Retry-After or backoff).
        """
        import time
        from . import loxo, loxoenrich
        time.sleep(120)
        while True:
            if loxo.configured():
                try:
                    loxoenrich.auto(DB(db_path), progress=log.appendleft)
                except Exception as e:  # noqa: BLE001
                    log.appendleft(f"Loxo contact info skipped: {e}")
            time.sleep(1800)

    if os.environ.get("ZOOMOLLO_NO_BACKGROUND") != "1":
        threading.Thread(target=loxo_contact_timer, daemon=True).start()

    def apollo_timer():
        """A few Apollo lookups a day for the best accounts, inside the free monthly credits."""
        import time
        from . import leads as _leads
        time.sleep(1800)
        while True:
            try:
                if _leads.configured():
                    _leads.backfill(DB(db_path), limit=60, progress=log.appendleft)
                    _leads.auto_enrich(DB(db_path), progress=log.appendleft)
            except Exception as e:  # noqa: BLE001
                log.appendleft(f"Apollo lookup skipped: {e}")
            time.sleep(6 * 3600)

    if os.environ.get("ZOOMOLLO_NO_BACKGROUND") != "1":
        threading.Thread(target=apollo_timer, daemon=True).start()

    def local_jobs(db) -> tuple[int, int]:
        """(open roles in your area, all open roles)."""
        from .hiringscore import _location_ok, load_desk
        stamp = db.stamp()
        if not db.conn.in_transaction and stamp[0] is not None and count_cache.get("jobs", (None,))[0] == stamp:
            return count_cache["jobs"][1]  # nothing written since the last count
        count_cache["jobs"] = (stamp, _local_jobs(db, _location_ok, load_desk(db)))
        return count_cache["jobs"][1]

    count_cache: dict = {}

    def _local_jobs(db, _location_ok, desk) -> tuple[int, int]:
        cities = {r[0]: r[1] for r in db.conn.execute("SELECT id, city FROM companies")}
        rows = db.conn.execute("SELECT title, location, company_id FROM jobs WHERE closed_at IS NULL").fetchall()
        local = sum(1 for j in rows if not desk.locations or _location_ok(j[1], cities.get(j[2]), desk, j[0]))
        return local, len(rows)

    def tab_counts(db) -> dict:
        """The number shown next to each tab name."""
        from . import today as td
        one = lambda q: db.conn.execute(q).fetchone()[0]  # noqa: E731
        try:
            n_today = len(td.call_list(db, limit=10_000, with_pitches=False))
        except Exception:  # noqa: BLE001 - a count must never break the page
            n_today = None
        try:
            from datetime import date as _d, timedelta as _td
            from . import scoreboard as sb
            ws = sb.week_start(_d.today())
            n_week = sb.tally(db, ws, ws + _td(days=6))["totals"]["dials"]
        except Exception:  # noqa: BLE001
            n_week = None
        return {"today_page": n_today, "scoreboard_page": n_week, "companies": one("SELECT COUNT(*) FROM companies"),
                "pipeline_page": one("SELECT COUNT(*) FROM pipeline WHERE stage NOT IN ('lost', 'not_now')"),
                "people": one("SELECT COUNT(*) FROM people"), "jobs": local_jobs(db)[0],
                "candidates": one("SELECT COUNT(*) FROM candidates"),
                "contacts": one("SELECT COUNT(*) FROM contacts")}

    def render(tab, tpl, **ctx):
        db = DB(db_path)
        from .offlimits import status as offlimits_status
        from .orgmap import linkedin_search_url
        cache: dict = {}

        def ol(cid):
            if cid not in cache:
                cache[cid] = offlimits_status(db, cid)
            return cache[cid]
        phones: dict = {}

        def cophone(cid):
            """A company's main line: its listed phone, else the best phone found on its site."""
            if cid is not None:
                from .today import _main_line
                if cid not in phones:
                    row = db.conn.execute("SELECT phone FROM companies WHERE id = ?", (cid,)).fetchone()
                    phones[cid] = _main_line(db, cid, row[0] if row else None)
                return phones[cid]
            if not phones:
                phones.update(db.conn.execute(
                    # US numbers first: a DFW desk calls the US office, not an overseas HQ line.
                    "SELECT co.id, COALESCE(NULLIF(co.phone, ''), (SELECT k.value FROM contacts k "
                    "WHERE k.company_id = co.id AND k.type = 'phone' ORDER BY CASE WHEN k.value LIKE '+1%' "
                    "THEN 0 ELSE 1 END, CASE k.kind WHEN 'company' THEN 0 "
                    "WHEN 'role' THEN 1 ELSE 2 END LIMIT 1)) FROM companies co").fetchall())
                phones.setdefault(None, None)
            return phones.get(cid)
        desk_cache: dict = {}

        def desk_fit(j) -> bool:
            """Does this job fit your desk (role + area)? Cheap; used to decide where to match candidates."""
            from .hiringscore import job_fit, load_desk
            if "d" not in desk_cache:
                desk_cache["d"] = load_desk(db)
                desk_cache["cities"] = {r[0]: r[1] for r in db.conn.execute("SELECT id, city FROM companies")}
            d = desk_cache["d"]
            return d.is_set and job_fit(j["title"], j["location"], desk_cache["cities"].get(j["company_id"]),
                                        j["salary_max"] if "salary_max" in j.keys() else None, d)[0]

        def pitch_for(j, n: int = 2):
            """Top candidates for a role — only for roles that fit your desk (keeps pages fast)."""
            from .candidates import candidates_for_job
            return candidates_for_job(db, j["id"], n) if desk_fit(j) else []

        from . import leads as _ap

        def apollo_left():
            return _ap.credits_left(db) if _ap.configured() else None
        body = render_template_string(tpl, depts=DEPT_LABELS, levels=LEVEL_LABELS, li_search=linkedin_search_url,
                                      apollo_left=apollo_left, **({"loxo_on": __import__("zoomollo.loxo", fromlist=["configured"]).configured()} | ctx),
                                      dict=dict, ol=ol, cophone=cophone, pitch_for=pitch_for, desk_fit=desk_fit)
        from .hiringscore import load_desk
        setup = []  # what's still missing before Zoomollo works properly for your desk
        if not load_desk(db).is_set:
            setup.append(("set your desk (roles you place, area)", url_for("companies")))
        if not (db.get_setting("my_name") or os.environ.get("MY_NAME")):
            setup.append(("add your name", url_for("messages")))
        if not (db.get_setting("my_address") or os.environ.get("MY_ADDRESS")):
            setup.append(("add your business mailing address (follow-up emails are on hold until then)",
                          url_for("messages")))
        import time as _t
        recent = [n for n in notices if _t.time() - n["at"] < 1800]  # finished in the last 30 minutes
        return render_template_string(PAGE, tab=tab, body=body, setup=setup, counts=tab_counts(db), notices=recent,
                                      version=started_label,
                                      busy=state["busy"] + tasks.qsize())

    @app.get("/")
    def today_page():
        from . import crm, today as td
        from .hiringscore import load_desk
        db = DB(db_path)
        rows = td.call_list(db, limit=int(request.args.get("n") or 25))
        carded = {r["company_id"] for r in rows}
        # A call-back that already has its card below isn't listed twice.
        due = [d for d in crm.due_items(db) if not (d["kind"] == "task" and d["company_id"] in carded)]
        import time as _t
        la = last_action.get("undo")
        show_undo = la if la and _t.time() - last_action.get("at", 0) < 900 else None
        from . import scoreboard as sb
        return render("today_page", TODAY, rows=rows, clean_role=td.clean_role, last_action=show_undo,
                      prog=sb.today_progress(db), lx=__import__("zoomollo.loxosync", fromlist=["status"]).status(db),
                      due=due, done=td.done_today(db), desk_set=load_desk(db).is_set)

    last_action: dict = {}  # the most recent logged outcome, so it can be undone (single-user app)

    @app.get("/opened")
    def opened():
        from . import crm
        db = DB(db_path)
        rows = db.conn.execute(
            "SELECT g.*, (SELECT subject FROM email_opens x WHERE COALESCE(x.email, x.name) = g.who "
            "  ORDER BY x.opened_at DESC LIMIT 1) AS subject, "
            " (SELECT name FROM companies c WHERE c.id = g.company_id) AS company_name, "
            " (SELECT title FROM people p WHERE p.id = g.person_id) AS title, "
            " (SELECT company FROM candidates k WHERE k.id = g.candidate_id) AS cand_company "
            "FROM (SELECT COALESCE(email, name) AS who, MAX(name) AS name, MAX(email) AS email, "
            "  MAX(person_id) AS person_id, MAX(candidate_id) AS candidate_id, MAX(company_id) AS company_id, "
            "  COUNT(*) AS times, MAX(opened_at) AS last FROM email_opens GROUP BY who) g "
            "ORDER BY g.last DESC").fetchall()
        return render("opened", OPENED, rows=rows, folder=os.environ.get("OPENS_FOLDER") or crm.OPENS_FOLDER,
                      checked=db.get_setting("inbox_checked_at"))

    @app.get("/scoreboard")
    def scoreboard_page():
        from . import loxo, loxosync, scoreboard as sb
        db = DB(db_path)
        return render("scoreboard_page", SCOREBOARD, w=sb.week(db), goal_labels=sb.GOAL_LABELS,
                      lx=loxosync.status(db), loxo_on=loxo.configured(), delay=loxosync.DELAY_MINUTES)

    @app.post("/loxo-sync")
    def loxo_sync_post():
        from . import loxosync
        db = DB(db_path)
        action = request.form.get("action")
        if action == "settings":
            db.set_setting("loxo_activity_sync", "1" if request.form.get("enabled") else "0")
            db.set_setting("loxo_create_contacts", "1" if request.form.get("create") else "0")
            db.commit()
        elif action == "retry":
            loxosync.retry_failed(db)
        if action in ("now", "retry"):
            # "Send now" skips the Undo wait for everything already logged.
            threading.Thread(target=lambda: loxosync.flush(DB(db_path), delay_minutes=0, progress=log.appendleft),
                             daemon=True).start()
            log.appendleft("✓ Sending logged activity to Loxo…")
        return redirect(url_for("scoreboard_page") + "#loxo")

    @app.post("/scoreboard/goals")
    def scoreboard_goals():
        from . import scoreboard as sb
        sb.save_goals(DB(db_path), request.form)
        return redirect(url_for("scoreboard_page"))

    @app.post("/today/log")
    def today_log():
        import time as _t
        from . import today as td
        f = request.form
        db = DB(db_path)
        try:
            undo = td.log_outcome(db, int(f.get("company", 0)), int(f["person"]) if f.get("person") else None,
                                  f.get("outcome", ""), f.get("note", ""))
        except ValueError:
            abort(400)
        co = db.company_row(undo["company_id"])
        undo["company"] = (co["name"] or co["key"]) if co else ""
        last_action.update(undo=undo, at=_t.time())
        if request.headers.get("X-Requested-With") == "fetch":  # saved from the page without a reload
            from . import scoreboard as sb
            return {"ok": True, "label": undo.get("label", ""), "company": undo["company"],
                    "prog": sb.today_progress(db), "reload": f.get("outcome") == "bad_number"}
        return redirect(url_for("today_page"))

    @app.post("/today/undo")
    def today_undo():
        from . import today as td
        if last_action.get("undo"):
            td.undo_outcome(DB(db_path), last_action["undo"])
            log.appendleft(f"✓ Undid: {last_action['undo']['label']} for {last_action['undo']['company']}")
            last_action.clear()
        return redirect(url_for("today_page"))

    # ---- call blitz ----------------------------------------------------------------------
    @app.get("/blitz")
    def blitz_page(preview=None, f=None):
        from . import blitz as bz
        db = DB(db_path)
        s = bz.load_session(db)
        live = s if s and s["i"] < len(s["items"]) else None  # a finished blitz isn't "in progress"
        return render("today_page", BLITZ_BUILD, f=f or bz.DEFAULTS, preview=preview, saved=bz.saved_lists(db),
                      session=live, st=bz.settings_of(db), describe=bz.describe,
                      verticals=bz.VERTICALS, who=bz.WHO, stages=bz.STAGE_FILTERS, sorts=bz.SORTS)

    @app.post("/blitz/start")
    def blitz_start():
        from datetime import datetime as _dt, timezone as _tz
        from . import blitz as bz
        db = DB(db_path)
        action = request.form.get("action", "start")
        saved = request.form.get("saved")
        if saved:
            if action == "delete":
                bz.delete_list(db, saved)
                return redirect(url_for("blitz_page"))
            f = bz.clean_filters(bz.saved_lists(db).get(saved) or {})
            name = saved
        else:
            f = bz.clean_filters(request.form)
            name = (request.form.get("list_name") or "").strip() or bz.describe(f)
        if action == "save":
            if not (request.form.get("list_name") or "").strip():
                return blitz_page(preview=None, f=f)
            bz.save_list(db, name, f)
            log.appendleft(f"✓ Saved blitz list: {name}")
            return redirect(url_for("blitz_page"))
        items = bz.build(db, f)
        if action == "preview" or not items:
            return blitz_page(preview=items, f=f)
        bz.save_session(db, {"name": name, "filters": f, "items": items, "i": 0, "log": [],
                             "started": _dt.now(_tz.utc).isoformat()})
        return redirect(url_for("blitz_run"))

    @app.post("/blitz/import")
    def blitz_import():
        import json as _json
        from . import csvimport as ci
        up = request.files.get("file")
        data = up.read(25_000_001) if up else b""
        if not data or len(data) > 25_000_000:
            log.appendleft("✗ CSV import: no file, or the file is over 25 MB")
            return redirect(url_for("blitz_page"))
        headers, rows = ci.read(data)
        if not headers or not rows:
            log.appendleft("✗ CSV import: the file has no rows")
            return redirect(url_for("blitz_page"))
        name = os.path.basename(up.filename or "CSV")[:80]
        db = DB(db_path)
        db.set_setting("csv_import_pending", _json.dumps({"filename": name, "headers": headers, "rows": rows}))
        db.commit()
        mapping = ci.guess(headers)
        samples = {f: [r[i] for r in rows[:3] if i is not None and r[i].strip()][:3]
                   for f, i in mapping.items()}
        return render("today_page", BLITZ_MAP, filename=name, headers=headers, mapping=mapping, samples=samples,
                      total=len(rows), capped=len(rows) >= ci.MAX_ROWS,
                      fields=[(f, lab) for f, (lab, _) in ci.FIELDS.items()])

    @app.post("/blitz/import/apply")
    def blitz_import_apply():
        import json as _json
        from . import csvimport as ci
        db = DB(db_path)
        try:
            p = _json.loads(db.get_setting("csv_import_pending") or "null")
        except ValueError:
            p = None
        if not p:
            return redirect(url_for("blitz_page"))
        f = request.form
        mapping = {}
        for k in ci.FIELDS:
            v = f.get(f"map_{k}", "")
            mapping[k] = int(v) if v.isdigit() and int(v) < len(p["headers"]) else None
        if mapping["company"] is None and mapping["website"] is None and mapping["email"] is None:
            log.appendleft("✗ CSV import: pick the Company or Website column")
            return redirect(url_for("blitz_page"))
        r = ci.apply(db, p["headers"], p["rows"], mapping, p["filename"], f.get("what", "").strip(),
                     f.get("city", "").strip())
        r["filename"] = p["filename"]
        r["crawling"] = 0
        if f.get("crawl"):
            for d in r["new_domains"][:300]:
                if submit("crawl", d, {}):
                    r["crawling"] += 1
        db.set_setting("csv_import_pending", "")
        db.set_setting("csv_import_last", _json.dumps({k: v for k, v in r.items() if k != "new_domains"}))
        db.commit()
        log.appendleft(f"✓ Imported {p['filename']}: {r['companies_new']} new companies, {r['people_new']} new people, "
                       f"{len(r['items'])} ready to call")
        if f.get("then") == "blitz" and r["items"]:
            return blitz_import_start()
        return render("today_page", BLITZ_IMPORTED, r=r)

    @app.post("/blitz/import/start")
    def blitz_import_start():
        import json as _json
        from datetime import datetime as _dt, timezone as _tz
        from . import blitz as bz
        db = DB(db_path)
        try:
            r = _json.loads(db.get_setting("csv_import_last") or "null")
        except ValueError:
            r = None
        if not r or not r.get("items"):
            return redirect(url_for("blitz_page"))
        bz.save_session(db, {"name": r["filename"], "filters": None, "items": r["items"], "i": 0, "log": [],
                             "started": _dt.now(_tz.utc).isoformat()})
        return redirect(url_for("blitz_run"))

    @app.post("/blitz/settings")
    def blitz_settings():
        db = DB(db_path)
        f = request.form
        db.set_setting("blitz_dialer", "tel" if f.get("dialer") == "tel" else "gv")
        acct = (f.get("gv_account") or "0").strip()
        db.set_setting("gv_account", acct if acct.isdigit() and len(acct) < 3 else "0")
        db.set_setting("my_phone", (f.get("my_phone") or "").strip()[:30])
        db.commit()
        return redirect(url_for("blitz_page"))

    @app.get("/blitz/run")
    def blitz_run():
        from datetime import datetime as _dt, timezone as _tz
        from . import blitz as bz, today as td
        db = DB(db_path)
        s = bz.load_session(db)
        if not s:
            return redirect(url_for("blitz_page"))
        labels = dict(td.OUTCOMES)
        if s["i"] >= len(s["items"]):
            try:
                mins = round((_dt.now(_tz.utc) - _dt.fromisoformat(s["started"])).total_seconds() / 60)
            except (KeyError, ValueError):
                mins = 0
            return render("today_page", BLITZ_DONE, s=s, t=bz.tally(s), labels=labels, minutes=mins)
        st = bz.settings_of(db)
        c = bz.card(db, s["items"][s["i"]], st)
        return render("today_page", BLITZ_RUN, s=s, c=c, t=bz.tally(s), st=st, labels=labels,
                      last=s["log"][-1] if s["log"] else None, clean_role=td.clean_role)

    @app.post("/blitz/log")
    def blitz_log():
        from datetime import datetime as _dt
        from . import blitz as bz, today as td
        db = DB(db_path)
        s = bz.load_session(db)
        f = request.form
        # The card number guards against a double key-press logging the same call twice.
        if not s or str(s["i"]) != f.get("i") or s["i"] >= len(s["items"]):
            return redirect(url_for("blitz_run"))
        item = s["items"][s["i"]]
        outcome = f.get("outcome", "")
        entry = {"company_id": item["company_id"], "company": item["company"], "person": item.get("person"),
                 "outcome": outcome, "note": (f.get("note") or "").strip()[:500],
                 "time": _dt.now().strftime("%I:%M %p").lstrip("0"), "undo": None}
        if outcome != "skip":
            try:
                entry["undo"] = td.log_outcome(db, item["company_id"], item.get("person_id"), outcome, entry["note"])
            except ValueError:
                abort(400)
        if outcome == "bad_number":
            entry["stay"] = True   # same account again, now showing its next number (or none)
        else:
            s["i"] += 1
        s["log"].append(entry)
        bz.save_session(db, s)
        return redirect(url_for("blitz_run"))

    @app.post("/blitz/undo")
    def blitz_undo():
        from . import blitz as bz, today as td
        db = DB(db_path)
        s = bz.load_session(db)
        if s and s["log"]:
            e = s["log"].pop()
            if e.get("undo"):
                td.undo_outcome(db, e["undo"])
            if not e.get("stay"):
                s["i"] = max(0, s["i"] - 1)
            bz.save_session(db, s)
        return redirect(url_for("blitz_run"))

    @app.post("/blitz/end")
    def blitz_end():
        from . import blitz as bz
        db = DB(db_path)
        s = bz.load_session(db)
        if s and s["i"] < len(s["items"]):
            # Ending early: keep the cards already worked and show the summary.
            s["items"] = s["items"][:s["i"]]
            if not s["items"]:
                bz.save_session(db, None)
                return redirect(url_for("blitz_page"))
            bz.save_session(db, s)
            return redirect(url_for("blitz_run"))
        bz.save_session(db, None)
        return redirect(url_for("blitz_page"))

    # Routine background lines that don't need you (shown only on request).
    _ROUTINE = re.compile(r"site didn't load|Résumés downloaded|^queued|^crawling |^reading directory|"
                          r"^Loxo (jobs|placements|people read|companies)|sync started|^https?://|"
                          r"database busy, retrying|^\[\d+/\d+\]", re.I)

    @app.get("/prospect")
    def find():
        lines = list(log)
        shown = lines if request.args.get("all") else [ln for ln in lines if not _ROUTINE.search(ln)]
        return render("find", FIND, log=shown, hidden_log=len(lines) - len(shown),
                      guesses=request.args.getlist("g"), bounced=request.args.getlist("b"),
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
                if submit("prospect", f"{what} in {where}",
                          {"what": what, "where": where, "limit": int(request.form.get("limit") or 25)}):
                    log.appendleft(f"queued: {what} in {where}")
            return redirect(url_for("find"))
        if action == "addco":
            site, what, where = (request.form.get(k, "").strip() for k in ("site", "what", "where"))
            if site:
                if submit("addco", site, {"what": what, "where": where}):
                    log.appendleft(f"queued: add {site}" + (f" ({what}, {where})" if what or where else ""))
            return redirect(url_for("find"))
        if action == "directory":
            what, where = request.form.get("what", "").strip(), request.form.get("where", "").strip()
            for it in [ln.strip() for ln in request.form.get("items", "").splitlines() if ln.strip()]:
                if where and submit("directory", it, {"what": what, "where": where,
                                                      "limit": int(request.form.get("limit") or 40)}):
                    log.appendleft(f"queued directory {it}" + (f" ({what})" if what else ""))
            return redirect(url_for("find"))
        for it in [ln.strip() for ln in request.form.get("items", "").splitlines() if ln.strip()]:
            if submit(action, it, {}):
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
        if f.get("channel") != "manual" and not 8 <= _local_hour() < 21:
            # Texting rules (TCPA and many states) limit marketing texts to 8am-9pm in the recipient's time.
            return Response("Not opened: texts are held outside 8am–9pm. Try again during business hours.", status=403)
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
        rows = db.people(q=q, department=d, hiring_only=h, limit=200)  # a 1,000-row page stalls a phone
        total = db.conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
        rows, line = phone_trim(rows, showing(len(rows), total, "people", bool(q or d or h), cap=200))
        return render("people", PEOPLE, rows=rows, q=q, dept=d, hiring=h, sequences=crm.sequences(db),
                      count_line=line, bulk_cap=BULK_CAP)

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
        from . import leads as _leads
        return render("companies", COMPANY, co=co, people=ppl, org=org, jobs=jobs, leads=_leads.leads(db, cid),
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
                      pin_set=bool(db.get_setting("remote_pin_hash")), pin_msg=request.args.get("pin_msg", ""),
                      ts_url=db.get_setting("tailscale_url"), auto=auto_info(db),
                      log=[ln for ln in list(log) if any(w in ln for w in ("Loxo", "Inbox", "Sent", "✗", "✓ "))
                           and not _ROUTINE.search(ln)][:8])

    def auto_info(db) -> dict:
        from . import autosend
        cfg = autosend.settings(db)
        hour = lambda h: f"{(h - 1) % 12 + 1}{'am' if h % 24 < 12 else 'pm'}"
        return {"cfg": cfg, "blockers": autosend.blockers(db), "note": db.get_setting("autosend_note"),
                "last": db.get_setting("autosend_last"), "next": autosend.pick_prospects(db, cfg["per_hour"]),
                "start": hour(cfg["start"]), "end": hour(cfg["end"]), "msg": request.args.get("auto_msg", "")}

    @app.post("/autosend")
    def autosend_post():
        from . import autosend
        db = DB(db_path)
        f = request.form
        action = f.get("action")
        try:
            vals = {k: int(f[k]) for k in ("per_hour", "start", "end") if f.get(k, "").strip()}
        except ValueError:
            vals = {}
        msg = ""
        if action == "off":
            autosend.save_settings(db, on=False, **vals)
            msg = "Auto-prospecting is off. Follow-ups already scheduled wait on the due list."
        elif autosend.blockers(db) and action in ("on", "now"):
            msg = "Not yet: " + " ".join(autosend.blockers(db))
        elif action == "on":
            cfg = autosend.save_settings(db, on=True, **vals)
            db.set_setting("autosend_hour", "")  # first batch on the next check, if inside sending hours
            msg = (f"Auto-prospecting is on: up to {cfg['per_hour']} emails an hour"
                   + ("." if autosend.in_window(cfg) else ", starting at the next sending hour."))
        elif action == "now":
            autosend.save_settings(db, **vals)
            tasks.put(("autosend", "auto-send", {"force": True}))
            msg = "Sending a batch now. It takes about a minute."
        else:
            autosend.save_settings(db, **vals)
            msg = "Saved."
        return redirect(url_for("pipeline_page", auto_msg=msg) + "#autosend")

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
        if not (f.get("q") or f.get("department") or f.get("hiring")):
            abort(400)  # never enrol the whole database by accident
        for p in db.people(q=f.get("q", ""), department=f.get("department") or None,
                           hiring_only=bool(f.get("hiring")), limit=BULK_CAP):
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

    # --- candidates -----------------------------------------------------------------
    @app.get("/candidates")
    def candidates():
        from . import candidates as cands, lens, loxo
        db = DB(db_path)
        q, source = request.args.get("q", "").strip(), request.args.get("source") or None
        if q:
            groups, results = cands.search(db, q, limit=60, source=source)
            for r in results:
                body = r["c"]["resume_text"] or "\n".join(x for x in [r["c"]["skills"], r["c"]["profile"]] if x)
                r["snips"] = lens.snippets(body, groups, limit=2) if body else []
        else:
            groups = []
            rows = db.conn.execute("SELECT * FROM candidates" + (" WHERE source = ?" if source else "") +
                                   " ORDER BY COALESCE(updated_at, created_at) DESC LIMIT 30", (source,) if source else ()).fetchall()
            results = [{"c": dict(r), "match": None, "fit": "", "snips": []} for r in rows]
        return render("candidates", CANDIDATES, q=q, src=source, groups=groups, results=results,
                      describe=lens.describe(groups), counts=cands.candidate_counts(db), loxo_on=loxo.configured(),
                      synced=db.get_setting("candidates_synced_at"), upload_msg=request.args.get("msg", ""))

    @app.get("/candidate/<int:cid>")
    def candidate(cid: int):
        import html as _html
        from . import candidates as cands, lens
        from .resumes import years_experience
        db = DB(db_path)
        c = db.conn.execute("SELECT * FROM candidates WHERE id = ?", (cid,)).fetchone()
        if not c:
            abort(404)
        q = request.args.get("q", "").strip()
        groups = lens.parse(q)
        text = c["resume_text"] or ""
        m = lens.match(cands.document(c), groups) if groups else None
        resume = (lens.highlight(text, groups) if groups else _html.escape(text)) if text else ""
        skills = lens.highlight(c["skills"] or "", groups) if groups else _html.escape(c["skills"] or "")
        if c["source"] == "loxo" and not text and c["resume_status"] in (None, "queued"):
            cands.RESUME_QUEUE.appendleft(cid)
        return render("candidates", CANDIDATE, c=c, q=q, groups=groups, m=m, resume=resume, skills=skills,
                      years=years_experience(text or c["profile"] or ""), jobs=cands.matching_jobs(db, cid))

    @app.post("/candidate/<int:cid>/fetch")
    def candidate_fetch(cid: int):
        from . import candidates as cands
        try:
            cands.fetch_resume(DB(db_path), cid)
        except Exception as e:  # noqa: BLE001
            return Response(f"Couldn't download the résumé: {e}", status=502)
        return redirect(request.referrer or url_for("candidate", cid=cid))

    @app.post("/candidates/upload")
    def candidates_upload():
        from . import candidates as cands
        db = DB(db_path)
        ok, failed = 0, []
        for f in request.files.getlist("files"):
            try:
                cands.add_upload(db, f.filename or "resume", f.read())
                ok += 1
            except Exception as e:  # noqa: BLE001
                failed.append(f"{f.filename}: {e}")
        msg = f"Added {ok} résumé{'s' if ok != 1 else ''}." + (" Couldn't read: " + "; ".join(failed) if failed else "")
        return redirect(url_for("candidates", msg=msg))

    @app.post("/candidates/tools")
    def candidates_tools():
        action = request.form.get("action")
        if action == "sync":
            threading.Thread(target=run_candidate_sync, daemon=True).start()
            log.appendleft("✓ Candidate sync started")
        elif action == "resumes":
            threading.Thread(target=run_resume_fill, args=(50,), daemon=True).start()
        return redirect(url_for("candidates"))

    @app.get("/pitch")
    def pitch_page():
        import os as _os
        from . import candidates as cands, crm
        from .offlimits import status as offlimits_status
        db = DB(db_path)
        cid, jid = int(request.args.get("candidate", 0)), int(request.args.get("job", 0))
        c = db.conn.execute("SELECT * FROM candidates WHERE id = ?", (cid,)).fetchone()
        job = db.conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
        if not c or not job:
            abort(404)
        co = db.company_row(job["company_id"])
        return render("candidates", PITCH, c=c, job=job, p=cands.pitch(db, cid, jid), q=request.args.get("q", ""),
                      company=co["name"] or co["key"], offl=offlimits_status(db, job["company_id"]),
                      smtp=crm.smtp_configured() and bool(_os.environ.get("SMTP_HOST")))

    def _pitched_in_loxo(db, f, job):
        """Also log 'Pitched' on the candidate's own Loxo record."""
        try:
            cand = int(f.get("candidate") or 0)
        except ValueError:
            cand = 0
        if not cand:
            return
        from .loxosync import enqueue_pitch
        from .today import clean_role
        co = db.company_row(job["company_id"])
        enqueue_pitch(db, cand, job["company_id"],
                      f"Pitched to {co['name'] if co else 'a client'} for {clean_role(job['title'])}")

    @app.post("/pitch")
    def pitch_send():
        from . import crm
        from .offlimits import status as offlimits_status
        db = DB(db_path)
        f = request.form
        job = db.conn.execute("SELECT * FROM jobs WHERE id = ?", (int(f.get("job", 0)),)).fetchone()
        if not job:
            abort(404)
        if offlimits_status(db, job["company_id"]).blocked and not f.get("override"):
            return Response("Not sent: this company is off-limits.", status=403)
        to, subject, body = f.get("to", "").strip(), f.get("subject", ""), f.get("body", "")
        person = db.conn.execute("SELECT id FROM people WHERE lower(email) = ?", (to.lower(),)).fetchone() if to else None
        pid = person["id"] if person else None
        if f.get("action") == "smtp" and to:
            try:
                mid = crm.send_smtp(db, to, subject, body)
            except Exception as e:  # noqa: BLE001
                return Response(f"Couldn't send: {e}", status=502)
            crm.record_email(db, to, subject, body, "smtp", pid, job["company_id"], message_id=mid)
            crm.log(db, job["company_id"], "pitch", subject, pid)  # counted on the Scoreboard
            db.commit()
            _pitched_in_loxo(db, f, job)
            return redirect(url_for("company", cid=job["company_id"]))
        if f.get("action") == "logsent" and to:
            # Only now, once you say you sent it, does it count as a touch.
            crm.record_email(db, to, subject, body, "mailto", pid, job["company_id"])
            crm.log(db, job["company_id"], "pitch", subject, pid)
            db.commit()
            _pitched_in_loxo(db, f, job)
            log.appendleft(f"✓ Pitch logged: {subject} → {to}")
            return redirect(url_for("today_page"))
        # Open the draft in your email app, then ask whether it was actually sent.
        return render("candidates", PITCH_SENT, mailto=crm.mailto_link(to, subject, body), to=to, subject=subject,
                      body=body, job=job["id"], override=f.get("override", ""), candidate=f.get("candidate", ""))

    @app.get("/companies")
    def companies():
        from . import crm, hiringscore
        db = DB(db_path)
        q, niche = request.args.get("q", ""), bool(request.args.get("niche"))
        rows = hiringscore.ranked(db, niche_only=niche, q=q)
        counts = {r[0]: r[1] for r in db.conn.execute("SELECT company_id, COUNT(*) FROM people GROUP BY company_id")}
        total = db.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        hiring = sum(1 for s, _ in rows if s.fit_roles)
        return render("companies", COMPANIES, rows=rows, q=q, niche=niche, counts=counts,
                      stage_names=crm.STAGE_NAMES, desk_card=desk_card(db),
                      count_line=showing(len(rows), total, "accounts", bool(q or niche), cap=500,
                                         extra=f"{hiring:,} hiring for roles on your desk"))

    @app.post("/desk")
    def desk_post():
        from .hiringscore import save_desk
        f = request.form
        save_desk(DB(db_path), f.get("titles", ""), f.get("exclude", ""), f.get("locations", ""), f.get("min_salary"),
                  f.get("max_open_roles", "keep"), f.get("min_headcount", "keep"), f.get("max_headcount", "keep"))
        return redirect(request.referrer or url_for("companies"))

    @app.post("/lead/<int:lid>/enrich")
    def lead_enrich(lid: int):
        from . import leads as _leads
        r = _leads.enrich(DB(db_path), lid)
        log.appendleft(("✓ Apollo: " if r["ok"] else "Apollo: ") + r["msg"])
        return redirect(request.referrer or url_for("today_page"))

    @app.post("/person/<int:pid>/loxo")
    def person_loxo(pid: int):
        """Add to Loxo, copy in anything Loxo already has, then open LinkedIn for the extension."""
        from . import loxoenrich
        from .orgmap import linkedin_search_url
        db = DB(db_path)
        p = db.conn.execute("SELECT p.*, co.name AS company_name, co.key AS company_key FROM people p "
                            "JOIN companies co ON co.id = p.company_id WHERE p.id = ?", (pid,)).fetchone()
        if not p:
            return redirect(url_for("today_page"))
        try:
            lid, how = loxoenrich.add(db, pid)
            got = loxoenrich.pull(db, pid) if lid else {}
            log.appendleft(f"✓ Loxo: {p['name']} {how}" + (f"; already had {', '.join(got.values())}" if got else "")
                           if lid else f"Loxo: {p['name']} not added ({how})")
        except Exception as e:  # noqa: BLE001
            log.appendleft(f"✗ Loxo: couldn't add {p['name']}: {e}")
        return redirect(p["linkedin_url"] or linkedin_search_url(p["name"], p["company_name"] or p["company_key"]))

    @app.post("/person/<int:pid>/loxo-channels")
    def person_loxo_channels(pid: int):
        """Add emails and phones Zoomollo found that this person's Loxo record does not have yet."""
        from . import loxoenrich
        db = DB(db_path)
        person = db.conn.execute("SELECT name FROM people WHERE id = ?", (pid,)).fetchone()
        if not person:
            abort(404)
        try:
            result = loxoenrich.push_channels(db, pid)
            if result.get("reason"):
                log.appendleft(f"Loxo: {person['name']} not updated ({result['reason']})")
            elif result["added"]:
                log.appendleft(f"✓ Loxo: added {', '.join(result['added'])} to {person['name']}")
            else:
                log.appendleft(f"✓ Loxo: {person['name']} already had those emails and phones")
        except Exception as e:  # noqa: BLE001
            log.appendleft(f"✗ Loxo: couldn't update {person['name']}: {e}")
        return redirect(request.referrer or url_for("today_page"))

    @app.post("/company/<int:cid>/leads")
    def company_leads(cid: int):
        from . import leads as _leads
        n = _leads.find_for_company(DB(db_path), cid, force=True)
        log.appendleft(f"✓ Apollo leaders: {n} found" if n is not None else "✗ Apollo leaders: add APOLLO_API_KEY to zoomollo-settings.txt")
        return redirect(url_for("company", cid=cid))

    @app.post("/company/<int:cid>/summary")
    def company_summary(cid: int):
        db = DB(db_path)
        text = " ".join((request.form.get("summary") or "").split())[:140]
        db.update_company(cid, summary=text or None, summary_source="you" if text else None)
        db.commit()
        return redirect(url_for("company", cid=cid))

    @app.post("/company/<int:cid>/headcount")
    def company_headcount(cid: int):
        """You know how big this company is: your number wins over the website's or Apollo's."""
        db = DB(db_path)
        if not db.company_row(cid):
            abort(404)
        digits = re.sub(r"[^\d]", "", request.form.get("headcount", ""))
        n = int(digits) if digits and int(digits) > 0 else None
        db.update_company(cid, headcount=n, headcount_source="you" if n else None)
        db.commit()
        return redirect(url_for("company", cid=cid))

    @app.get("/contacts")
    def contacts():
        q, t, k = request.args.get("q", ""), request.args.get("type") or None, request.args.get("kind") or None
        db = DB(db_path)
        rows = db.search(q, t, k)
        total = db.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        rows, line = phone_trim(rows, showing(len(rows), total, "contact details", bool(q or t or k), cap=500))
        return render("contacts", CONTACTS, rows=rows, q=q, type_=t, kind=k, count_line=line)

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
        # Roles outside your area are hidden unless asked for (a DFW desk doesn't need Bengaluru).
        anywhere = bool(request.args.get("anywhere"))
        hidden = 0
        if desk.locations and not anywhere:
            from .hiringscore import _location_ok
            keep = [j for j in rows if _location_ok(j["location"], cities.get(j["company_id"]), desk, j["title"])]
            hidden, rows = len(rows) - len(keep), keep
        total = db.conn.execute("SELECT COUNT(*) FROM jobs WHERE closed_at IS NULL").fetchone()[0]
        fit_n = sum(1 for j in rows if fits[j["id"]][0])
        extra = ", ".join(x for x in [f"{fit_n:,} fit your desk" if desk.titles else "",
                                      f"{hidden:,} outside your area hidden" if hidden else ""] if x)
        rows = sorted(rows, key=lambda j: not fits[j["id"]][0])  # roles on your desk first, newest first within each
        rows, line = phone_trim(rows, showing(len(rows), total, "open roles", bool(q or niche or hidden), cap=500, extra=extra))
        return render("jobs", JOBS, rows=rows, q=q, niche=niche, fits=fits, desk_set=desk.is_set,
                      count_line=line,
                      anywhere=anywhere, hidden=hidden, area="; ".join(desk.locations),
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

    @app.post("/company/<int:cid>/website")
    def company_website(cid: int):
        from .crawler import normalize_domain
        site = request.form.get("website", "").strip()
        if not site or "." not in normalize_domain(site):
            return redirect(url_for("company", cid=cid))
        db = DB(db_path)
        if not db.company_row(cid):
            abort(404)
        keep = db.set_website(cid, site)
        if submit("crawl", normalize_domain(site), {}):
            log.appendleft(f"Website added: {normalize_domain(site)}, reading it now")
        return redirect(url_for("company", cid=keep, added=1))

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
