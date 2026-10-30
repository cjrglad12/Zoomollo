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
    pr.add_argument("--no-people", action="store_true", help="Skip search-engine people lookups")
    pr.add_argument("--no-osm", action="store_true", help="Skip OpenStreetMap")

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
    ve = sub.add_parser("verify", help="Check guesses with NeverBounce/ZeroBounce, re-guessing until valid "
                                       "or 10 tries")
    ve.add_argument("query", nargs="?", default="", help="Limit to matching people/companies")
    ve.add_argument("--hiring", action="store_true", help="Only likely hiring managers")
    ve.add_argument("--limit", type=int, default=50)

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
    s.add_argument("--kind", choices=["person", "role", "company", "guessed"])
    s.add_argument("--limit", type=int, default=100)

    sub.add_parser("companies", help="List companies and counts")
    sub.add_parser("stats", help="Database totals")

    e = sub.add_parser("export", help="Export contacts to CSV (Loxo/CRM import ready)")
    e.add_argument("-o", "--out", default="-")
    e.add_argument("query", nargs="?", default="")
    e.add_argument("--type", choices=["email", "phone"])
    e.add_argument("--kind", choices=["person", "role", "company", "guessed"])
    e.add_argument("--people", action="store_true", help="Export people (org map) instead of raw contacts")
    e.add_argument("--hiring", action="store_true", help="With --people: hiring managers only")
    e.add_argument("--jobs", action="store_true", help="Export open roles x matched contacts")

    x = sub.add_parser("suppress", help="Opt someone out: deletes and blocks an email, phone or @domain")
    x.add_argument("value")
    x.add_argument("--reason", default="opt-out")

    lp = sub.add_parser("loxo-push", help="Push named contacts to Loxo (dry run unless --send)")
    lp.add_argument("--send", action="store_true")
    lp.add_argument("--include-guessed", action="store_true")
    lp.add_argument("--hiring", action="store_true", help="Only likely hiring managers")
    lp.add_argument("--limit", type=int, default=100)

    sv = sub.add_parser("serve", help="Run the web UI")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8080)

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(message)s")
    db = DB(a.db)

    if a.cmd == "prospect":
        from .search import get_provider
        provider = get_provider()
        if not provider:
            print("Note: no search API configured (BRAVE_API_KEY / SERPAPI_KEY / SEARXNG_URL), so only "
                  "OpenStreetMap and company websites are used. See README.", file=sys.stderr)
        res = pipeline.prospect(db, a.what, a.where, a.limit, a.pages, provider,
                                find_people=not a.no_people, use_osm=not a.no_osm, progress=print)
        print(json.dumps(res, indent=2))

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

    elif a.cmd == "verify":
        from . import emailcheck
        verifier = emailcheck.get_verifier()
        if not verifier:
            print("Set NEVERBOUNCE_API_KEY or ZEROBOUNCE_API_KEY first.", file=sys.stderr)
            return 1
        rows = [p for p in db.people(q=a.query, hiring_only=a.hiring, limit=10_000)
                if p["email_status"] in (None, "guessed")][: a.limit]
        for p in rows:
            email, status = emailcheck.auto_verify(db, p["id"], verifier)
            print(f"{p['name']} ({p['company_name']}): {email or '-'} [{status}]")

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
        create_app(db.path).run(host=a.host, port=a.port)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
