"""Command line: `zoomollo --help`."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import pipeline
from .db import DB


def _print_rows(rows, cols):
    for r in rows:
        print(" | ".join(str(r[c] or "") for c in cols))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="zoomollo", description="Self-hosted contact discovery.")
    ap.add_argument("--db", help="SQLite file (default: $ZOOMOLLO_DB or ./zoomollo.db)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("prospect", help='Find companies by industry + city, then map their people. '
                        'e.g. zoomollo prospect "commercial HVAC" "Austin, TX"')
    pr.add_argument("what", help="What the companies do")
    pr.add_argument("where", help="US city, e.g. 'Austin, TX'")
    pr.add_argument("--limit", type=int, default=25, help="Max companies (default 25)")
    pr.add_argument("--pages", type=int, default=15, help="Pages to crawl per company site")
    pr.add_argument("--no-osm", action="store_true", help="Skip map data")
    pr.add_argument("--no-groups", action="store_true", help="Skip chamber / association member directories")
    pr.add_argument("--no-sources", action="store_true", help="Skip your saved public data sources")
    pr.add_argument("--no-records", action="store_true",
                    help="Skip automatic license / permit records from open-data portals")
    pr.add_argument("--no-links", action="store_true", help="Don't follow links between company sites")

    so = sub.add_parser("sources", help="Public data files (license lists, permits) used on every prospect run")
    so_sub = so.add_subparsers(dest="sources_cmd", required=True)
    sa = so_sub.add_parser("add", help="Add a CSV/JSON link or file")
    sa.add_argument("url")
    sa.add_argument("--label")
    so_sub.add_parser("list")
    sr = so_sub.add_parser("remove")
    sr.add_argument("id", type=int)
    st = so_sub.add_parser("test", help="Show what a source would give for a search, without saving")
    st.add_argument("url")
    st.add_argument("what")
    st.add_argument("where")

    pe = sub.add_parser("people", help="List mapped people (org charts)")
    pe.add_argument("query", nargs="?", default="")
    pe.add_argument("--department")
    pe.add_argument("--hiring", action="store_true", help="Only likely hiring managers")
    pe.add_argument("--limit", type=int, default=200)

    hi = sub.add_parser("hiring", help="Open roles cross-referenced with contacts")
    hi.add_argument("query", nargs="?", default="")
    hi.add_argument("--limit", type=int, default=200)

    eb = sub.add_parser("email-bad", help="Mark email(s) wrong/bounced and move to the next guess")
    eb.add_argument("emails", nargs="+")
    eg = sub.add_parser("email-good", help="Mark email(s) correct; locks in that company's format")
    eg.add_argument("emails", nargs="+")
    bo = sub.add_parser("bounces", help="Import bounce notices / a list of bad addresses from a file ('-' = stdin)")
    bo.add_argument("file")

    tx = sub.add_parser("text", help="Print a filled-in text and a link that opens your texting app")
    tx.add_argument("person_id", type=int)
    tx.add_argument("--template", type=int, help="Template id (default: the first one)")

    ls_ = sub.add_parser("loxo-sync", help="Refresh Loxo clients, jobs and placements for off-limits checks")
    ls_.add_argument("--quick", action="store_true", help="Jobs and placements only (fast)")
    ol = sub.add_parser("offlimits", help="Check, mark or clear a company as off-limits")
    ol.add_argument("company", help="Domain or company key")
    ol.add_argument("--set", metavar="REASON")
    ol.add_argument("--until", help="YYYY-MM-DD")
    ol.add_argument("--clear", action="store_true")
    dk = sub.add_parser("desk", help="Show or set your niche (roles you place, areas, salary floor)")
    dk.add_argument("--titles", help='e.g. "HVAC technician, service manager"')
    dk.add_argument("--exclude", help='e.g. "intern, driver"')
    dk.add_argument("--locations", help='e.g. "Austin, TX; MO"')
    dk.add_argument("--min-salary")
    tp = sub.add_parser("top", help="Companies ranked by hiring score")
    tp.add_argument("--limit", type=int, default=20)
    tp.add_argument("--niche", action="store_true", help="Only companies with roles in your niche")
    rc = sub.add_parser("recheck", help="Re-check companies that are due and record what changed")
    rc.add_argument("--limit", type=int, default=20)
    rc.add_argument("--days", type=int, help="Re-check companies not checked for this many days (default 7)")
    ch = sub.add_parser("changes", help="New, reposted and closed roles and new leaders")
    ch.add_argument("--days", type=int, default=7)
    ch.add_argument("--niche", action="store_true")
    cs = sub.add_parser("candidates", help='Search candidates through the keyword lens, e.g. "tax manager, review, TX | WI"')
    cs.add_argument("query")
    cs.add_argument("--limit", type=int, default=20)
    sub.add_parser("candidates-sync", help="Sync candidates from Loxo (résumés download separately)")
    rs = sub.add_parser("resumes", help="Download Loxo résumés now")
    rs.add_argument("--limit", type=int, default=100)
    ad = sub.add_parser("add-resumes", help="Add résumé files (PDF, .docx, .txt) as candidates")
    ad.add_argument("files", nargs="+")
    sub.add_parser("pipeline", help="Show the client pipeline by stage")
    sub.add_parser("due", help="Follow-ups and next steps due today")
    sub.add_parser("send-due", help="Send due follow-up emails from your mailbox (SMTP settings)")
    sub.add_parser("inbox", help="Check your inbox for replies, unsubscribes and bounces (IMAP settings)")

    c = sub.add_parser("crawl", help="Crawl company websites for emails and phones")
    c.add_argument("domains", nargs="+", help="Domains, or @file.txt with one per line")
    c.add_argument("--max-pages", type=int, default=40)
    c.add_argument("--delay", type=float, default=1.0, help="Seconds between requests to a site")
    c.add_argument("--region", default="US", help="Default phone region (ISO country code)")

    j = sub.add_parser("jobs", help="Pull open roles + contacts from a public job board")
    j.add_argument("refs", nargs="+", help="board:slug, e.g. greenhouse:stripe lever:netflix ashby:openai")
    j.add_argument("--domain", help="Company domain to file the jobs under")

    d = sub.add_parser("detect", help="Find which job board a company slug uses")
    d.add_argument("slug")

    g = sub.add_parser("guess", help="Guess someone's work email from name + domain")
    g.add_argument("name")
    g.add_argument("domain")
    g.add_argument("--title")
    g.add_argument("--no-save", action="store_true")

    s = sub.add_parser("search", help="Search saved contacts")
    s.add_argument("query", nargs="?", default="")
    s.add_argument("--type", choices=["email", "phone"])
    s.add_argument("--kind", choices=["person", "company", "guessed"])
    s.add_argument("--limit", type=int, default=100)

    sub.add_parser("companies", help="List companies and counts")
    sub.add_parser("stats", help="Database totals")

    e = sub.add_parser("export", help="Export contacts to CSV (Loxo/CRM import ready)")
    e.add_argument("-o", "--out", default="-")
    e.add_argument("query", nargs="?", default="")
    e.add_argument("--type", choices=["email", "phone"])
    e.add_argument("--kind", choices=["person", "company", "guessed"])
    e.add_argument("--people", action="store_true", help="Export people (org map) instead of raw contacts")
    e.add_argument("--hiring", action="store_true", help="With --people: hiring managers only")
    e.add_argument("--jobs", action="store_true", help="Export open roles x matched contacts")

    x = sub.add_parser("suppress", help="Opt someone out: deletes and blocks an email, phone or @domain")
    x.add_argument("value")
    x.add_argument("--reason", default="opt-out")

    bl = sub.add_parser("block", help="Block a company (e.g. a staffing/recruiting firm) and delete its data")
    bl.add_argument("companies", nargs="+", help="Domain or company key")
    sub.add_parser("purge", help="Remove generic inboxes, recruiting people and staffing firms already saved")

    lp = sub.add_parser("loxo-push", help="Push named contacts to Loxo (dry run unless --send)")
    lp.add_argument("--send", action="store_true")
    lp.add_argument("--include-guessed", action="store_true")
    lp.add_argument("--hiring", action="store_true", help="Only likely hiring managers")
    lp.add_argument("--limit", type=int, default=100)

    sv = sub.add_parser("serve", help="Run the web UI")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8080)
    sv.add_argument("--open", action="store_true", help="Open the web UI in your browser")

    from .settings import load_settings
    load_settings()
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(message)s")
    db = DB(a.db)

    if a.cmd == "prospect":
        res = pipeline.prospect(db, a.what, a.where, a.limit, a.pages, use_osm=not a.no_osm,
                                use_hubs=not a.no_groups, use_sources=not a.no_sources, use_records=not a.no_records,
                                follow_links=not a.no_links, progress=print)
        print(json.dumps(res, indent=2))

    elif a.cmd == "sources":
        if a.sources_cmd == "add":
            print(f"Added source #{db.add_source(a.url, a.label)}")
        elif a.sources_cmd == "remove":
            db.remove_source(a.id)
            print(f"Removed source #{a.id}")
        elif a.sources_cmd == "test":
            from .datasources import from_source
            found = from_source(a.url, a.what, a.where)
            for c in found[:50]:
                print(" | ".join(x or "" for x in [c.name, c.domain, c.phone, c.address, c.contact_name]))
            print(f"{len(found)} matching companies")
        else:
            _print_rows(db.sources(), ["id", "label", "url", "last_used"])

    elif a.cmd == "people":
        _print_rows(db.people(q=a.query, department=a.department, hiring_only=a.hiring, limit=a.limit),
                    ["company_name", "name", "title", "department", "email", "email_status", "hiring_for"])

    elif a.cmd == "hiring":
        from .xref import hiring_rows
        last = None
        for r in hiring_rows(db, a.query, limit=a.limit):
            if r["job_id"] != last:
                print(f"\n{r['company_name'] or r['company_key']}: {r['job_title']} ({r['location'] or '-'})")
                last = r["job_id"]
            if r["reason"]:
                who = r["name"] or r["value"]
                extra = " ".join(x for x in [r["title"] and f"({r['title']})", r["email"], r["phone"]] if x)
                print(f"   [{r['reason']}] {who} {extra}")

    elif a.cmd in ("email-bad", "email-good", "bounces"):
        from . import emailcheck
        if a.cmd == "email-good":
            for e_ in a.emails:
                n = emailcheck.mark_good(db, e_)
                print(f"✓ {e_} confirmed; re-guessed {n} colleague(s) with that format")
        else:
            if a.cmd == "bounces":
                text = sys.stdin.read() if a.file == "-" else open(a.file).read()
                results = emailcheck.import_bounces(db, text)
            else:
                results = [(e_, name, nxt) for e_ in a.emails for name, nxt in emailcheck.mark_bad(db, e_)]
            for bad, name, nxt in results:
                print(f"✗ {bad} ({name}) -> " + (f"next guess {nxt}" if nxt else "no guesses left (failed)"))
            if not results:
                print("No saved people had those addresses (they're still blocked from future guesses).")

    elif a.cmd == "block":
        from .crawler import normalize_domain
        for k in a.companies:
            row = db.conn.execute("SELECT id FROM companies WHERE key = ? OR domain = ?",
                                  (k, normalize_domain(k))).fetchone()
            if row:
                db.block_company(row["id"], "blocked by user")
            else:
                db.conn.execute("INSERT OR REPLACE INTO blocked_companies (key, reason, added_at) "
                                "VALUES (?, 'blocked by user', datetime('now'))", (normalize_domain(k),))
                db.commit()
            print(f"Blocked {k}")

    elif a.cmd == "purge":
        print(json.dumps(db.purge_excluded(), indent=2))

    elif a.cmd == "text":
        from . import messaging
        messaging.ensure_starters(db)
        p = db.conn.execute("SELECT * FROM people WHERE id = ?", (a.person_id,)).fetchone()
        if not p or not p["phone"]:
            print("That person has no phone number saved.", file=sys.stderr)
            return 1
        tpl = db.template(a.template) if a.template else db.templates()[0]
        text, missing = messaging.render(tpl["body"], messaging.person_fields(db, p["id"]))
        print(text + ("\n(filled generically: " + ", ".join(missing) + ")" if missing else ""))
        print(messaging.sms_link(p["phone"], text))

    elif a.cmd == "loxo-sync":
        from . import loxo
        print(json.dumps(loxo.sync(db, progress=print, full=not a.quick), indent=2))

    elif a.cmd == "offlimits":
        from .crawler import normalize_domain
        from .offlimits import set_manual, status
        row = db.conn.execute("SELECT id FROM companies WHERE key = ? OR domain = ?",
                              (a.company, normalize_domain(a.company))).fetchone()
        if not row:
            print("Company not found.", file=sys.stderr)
            return 1
        if a.clear:
            set_manual(db, row["id"], None)
        elif a.set:
            set_manual(db, row["id"], a.set, a.until)
        st = status(db, row["id"])
        print(f"{st.level or 'ok'}: {st.reason or 'no off-limits rules apply'}")

    elif a.cmd == "desk":
        from . import hiringscore
        d = hiringscore.load_desk(db)
        if any(v is not None for v in (a.titles, a.exclude, a.locations, a.min_salary)):
            d = hiringscore.save_desk(db, a.titles if a.titles is not None else ", ".join(d.titles),
                                      a.exclude if a.exclude is not None else ", ".join(d.exclude),
                                      a.locations if a.locations is not None else "; ".join(d.locations),
                                      a.min_salary if a.min_salary is not None else d.min_salary)
        print(json.dumps(d.__dict__, indent=2))

    elif a.cmd == "top":
        from . import hiringscore
        for sc, co in hiringscore.ranked(db, niche_only=a.niche, limit=a.limit):
            why = sc.offlimits.reason if sc.offlimits.blocked else " · ".join(sc.top_reasons)
            print(f"{sc.total:>3}  {co['name'] or co['key']}  | {why}")

    elif a.cmd == "recheck":
        from . import recheck
        print(json.dumps(recheck.run(db, a.limit, a.days, progress=print)))

    elif a.cmd == "changes":
        from . import recheck
        for kind, label in recheck.KINDS.items():
            rows = recheck.changes(db, a.days, a.niche)[kind]
            if rows:
                print(f"\n{label} ({len(rows)})")
                for e in rows:
                    print(f"   {e['created_at'][:10]}  {e['company_name'] or e['company_key']}: {e['detail']}")

    elif a.cmd == "candidates":
        from . import candidates as cands
        groups, results = cands.search(db, a.query, limit=a.limit)
        print("Keywords: " + " · ".join(g.label for g in groups))
        for r in results:
            c, m = r["c"], r["match"]
            chips = "  ".join(f"[{g.label}: {n}]" if n else f"({g.label}: -)" for g, n in zip(groups, m.hits))
            print(f"{c['name'] or '?'} | {c['title'] or ''} | {', '.join(x for x in [c['city'], c['state']] if x)}"
                  f"{' | ' + r['fit'] if r['fit'] else ''}\n   {chips}")

    elif a.cmd == "candidates-sync":
        from . import candidates as cands
        print(json.dumps(cands.sync_loxo(db, progress=print)))

    elif a.cmd == "resumes":
        from . import candidates as cands
        print(f"Downloaded {cands.fill_resumes(db, limit=a.limit, progress=print)}")

    elif a.cmd == "add-resumes":
        from pathlib import Path
        from . import candidates as cands
        for f in a.files:
            try:
                cands.add_upload(db, Path(f).name, Path(f).read_bytes())
                print(f"✓ {f}")
            except Exception as e:  # noqa: BLE001
                print(f"✗ {f}: {e}")

    elif a.cmd == "pipeline":
        from . import crm
        for key, label in crm.STAGES:
            cards = crm.board(db)[key]
            if cards:
                print(f"\n{label} ({len(cards)})")
                for c in cards:
                    extra = " | ".join(x for x in [c["contact_name"], c["next_step"] and f"{c['next_step']} ({c['due_date'] or 'no date'})",
                                                   c["offlimits"].reason] if x)
                    print(f"   {c['company_name'] or c['company_key']}" + (f" | {extra}" if extra else ""))

    elif a.cmd == "due":
        from . import crm
        for it in crm.due_items(db):
            if it["kind"] == "task":
                print(f"[next step] {it['company_name']}: {it['next_step']} (due {it['due']})")
            else:
                print(f"[{it['channel']} step {it['step_no']}] {it['name']} ({it['company_name']}) -> {it['to']}"
                      + (f" | {it['subject']}" if it["subject"] else ""))

    elif a.cmd == "send-due":
        from . import crm
        if not crm.smtp_configured():
            print("Add SMTP_HOST, SMTP_USER and SMTP_PASSWORD to zoomollo-settings.txt first.", file=sys.stderr)
            return 1
        print(json.dumps(crm.send_due_emails(db, pause=8.0, progress=print)))

    elif a.cmd == "inbox":
        from . import crm
        if not crm.imap_configured():
            print("Add IMAP_HOST (plus SMTP_USER / SMTP_PASSWORD) to zoomollo-settings.txt first.", file=sys.stderr)
            return 1
        print(json.dumps(crm.check_inbox(db)))

    elif a.cmd == "crawl":
        domains = []
        for d_ in a.domains:
            if d_.startswith("@"):
                with open(d_[1:]) as f:
                    domains += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
            else:
                domains.append(d_)
        for dom in domains:
            print(f"== {dom}")
            res = pipeline.crawl_company(db, dom, a.max_pages, a.delay, a.region,
                                         progress=(lambda m: print("  ", m)) if a.verbose else None)
            print(f"   {res['pages']} pages, {res['emails']} emails, {res['phones']} phones")

    elif a.cmd == "jobs":
        for ref in a.refs:
            board, _, slug = ref.partition(":")
            try:
                res = pipeline.import_jobs(db, board, slug, a.domain)
            except Exception as ex:  # noqa: BLE001
                print(f"{ref}: failed ({ex})", file=sys.stderr)
                continue
            print(f"{ref}: {res['jobs']} jobs, {res['emails']} emails, {res['phones']} phones")

    elif a.cmd == "detect":
        from .jobboards import detect_board
        found = detect_board(a.slug)
        print("\n".join(f"{b}:{a.slug} ({n} jobs)" for b, n in found) or "No public board found.")

    elif a.cmd == "guess":
        for i, e_ in enumerate(pipeline.guess_for_person(db, a.name, a.domain, a.title, not a.no_save)):
            print(("* " if i == 0 else "  ") + e_)

    elif a.cmd == "search":
        _print_rows(db.search(a.query, a.type, a.kind, a.limit),
                    ["company_key", "name", "title", "value", "kind"])

    elif a.cmd == "companies":
        _print_rows(db.companies(), ["key", "name", "emails", "phones", "open_jobs", "email_pattern"])

    elif a.cmd == "stats":
        print(json.dumps(db.stats(), indent=2))

    elif a.cmd == "export":
        data = (pipeline.export_hiring_csv(db, a.query) if a.jobs
                else pipeline.export_people_csv(db, a.query, hiring_only=a.hiring) if a.people
                else pipeline.export_csv(db, a.query, a.type, a.kind))
        if a.out == "-":
            sys.stdout.write(data)
        else:
            with open(a.out, "w", newline="") as f:
                f.write(data)
            print(f"Wrote {a.out}")

    elif a.cmd == "suppress":
        n = db.suppress(a.value, a.reason)
        print(f"Suppressed {a.value} (removed {n} saved contact(s))")

    elif a.cmd == "loxo-push":
        from .loxo import push_people
        sent = push_people(db, dry_run=not a.send, include_guessed=a.include_guessed,
                           hiring_only=a.hiring, limit=a.limit)
        for p in sent:
            print(json.dumps(p))
        print(f"{'Pushed' if a.send else 'Would push'} {len(sent)} people.")

    elif a.cmd == "serve":
        from .web import create_app
        url = f"http://{'127.0.0.1' if a.host in ('0.0.0.0', '127.0.0.1') else a.host}:{a.port}"
        print(f"\nZoomollo is running at {url}\nLeave this window open while you use it. "
              f"Close it (or press Ctrl+C) to stop.\n")
        if a.open:
            import threading
            import webbrowser
            threading.Timer(1.5, lambda: webbrowser.open(url)).start()
        import flask.cli
        logging.getLogger("werkzeug").setLevel(logging.ERROR)  # quiet per-request logs
        flask.cli.show_server_banner = lambda *args, **kw: None
        create_app(db.path).run(host=a.host, port=a.port)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
