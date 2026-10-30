# Zoomollo

Self-hosted contact discovery: a do-it-yourself ZoomInfo/Apollo. It crawls company websites and public job boards for business emails and phone numbers, learns each company's email format, guesses addresses for named people, and keeps everything in one local SQLite file you can search in a web UI, export to CSV or push to Loxo.

## Quick start

```bash
pip install -e .            # Python 3.10+
zoomollo serve              # web UI at http://127.0.0.1:8080
```

Or from the command line:

```bash
zoomollo crawl acme.com globex.io          # crawl sites (or: zoomollo crawl @domains.txt)
zoomollo jobs greenhouse:stripe lever:plaid ashby:notion workable:hotjar
zoomollo detect airbnb                      # which job board does a company use?
zoomollo guess "Jane Doe" acme.com          # best-guess work email
zoomollo search "vp sales"                  # search saved contacts
zoomollo export -o contacts.csv --kind person
zoomollo suppress jane@acme.com             # opt-out: delete + never store again
zoomollo loxo-push                          # dry run; add --send to push for real
```

The database is `./zoomollo.db` unless you pass `--db` or set `ZOOMOLLO_DB`.

## Where the data comes from

| Source | How | What you get |
|---|---|---|
| Company websites | Crawler goes for contact, team, about, leadership and careers pages first. Reads `mailto:`/`tel:` links, page text, obfuscated addresses (`jane [at] acme [dot] com`) and schema.org `Person` data. | Emails, phones, names and titles, social profile links |
| Job boards | Official public JSON feeds for Greenhouse, Lever, Ashby and Workable | Open roles (hiring signals) plus recruiter emails and phones in job posts |
| Email format | Learned from named emails found on a company's site (e.g. `first.last`, 90% of examples) | Guessed addresses for any name at that company, labelled `guessed` |

Every contact is labelled by kind: `person` (seen with a name), `role` (info@, careers@…), `company` (main phone lines) or `guessed`, and stores the page it was found on.

## Loxo

Set `LOXO_AGENCY_SLUG` and `LOXO_API_KEY` (and `LOXO_DOMAIN` if you're not on `app.loxo.co`). `zoomollo loxo-push` sends named contacts that haven't been pushed yet as Loxo people. It's a dry run by default so you can check the payload; confirm the field names against your Loxo API docs before the first `--send`. CSV export also works with Loxo's importer.

## What it doesn't do (on purpose)

- **No LinkedIn, Facebook or Instagram scraping.** Their terms forbid it, they block it aggressively, and accounts get banned. Social profile links found on company sites are saved, but the profiles aren't crawled.
- **No CAPTCHA or login bypassing.** It identifies itself, follows `robots.txt` and waits between requests (1s per site by default).
- **No SMTP "pinging" to verify mailboxes.** Most cloud hosts block port 25 and it hurts your sending domain's reputation. Guesses only check that the domain accepts mail (MX record). Plug in a verifier such as NeverBounce or ZeroBounce for that.

## Compliance

You're responsible for how you use the data. Business contact data used for B2B outreach is generally allowed in the US (follow CAN-SPAM: honest headers and an unsubscribe). For people in the EU/UK (GDPR) or California (CCPA) you need a lawful basis, must tell people where you got their data, and must honour deletion requests. Use `zoomollo suppress` for opt-outs; suppressed emails, phones or whole `@domains` are deleted and never stored again.

## Ideas for next steps

- Search-engine discovery: feed a search API (Brave, SerpAPI, Bing) queries like `"acme" "head of sales"` to find names, then guess emails.
- More job boards: SmartRecruiters, Recruitee, BambooHR, JazzHR, Indeed/LinkedIn via their partner APIs.
- Company discovery from lists (Crunchbase export, state business registries, conference sponsor pages).
- Email verification API, scheduled re-crawls, and Postgres for multi-user use.

## Development

```bash
pip install -e '.[dev]'
pytest
```
