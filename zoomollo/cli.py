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

    x = sub.add_parser("suppress", help="Opt someone out: deletes and blocks an email, phone or @domain")
    x.add_argument("value")
    x.add_argument("--reason", default="opt-out")

    lp = sub.add_parser("loxo-push", help="Push named contacts to Loxo (dry run unless --send)")
    lp.add_argument("--send", action="store_true")
    lp.add_argument("--include-guessed", action="store_true")
    lp.add_argument("--limit", type=int, default=100)

    sv = sub.add_parser("serve", help="Run the web UI")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8080)

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(message)s")
    db = DB(a.db)

    if a.cmd == "crawl":
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
        data = pipeline.export_csv(db, a.query, a.type, a.kind)
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
        sent = push_people(db, dry_run=not a.send, include_guessed=a.include_guessed, limit=a.limit)
        for p in sent:
            print(json.dumps(p))
        print(f"{'Pushed' if a.send else 'Would push'} {len(sent)} people.")

    elif a.cmd == "serve":
        from .web import create_app
        create_app(db.path).run(host=a.host, port=a.port)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
