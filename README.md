# Zoomollo

Self-hosted contact discovery: a do-it-yourself ZoomInfo/Apollo. Tell it what kind of companies and which city ("commercial HVAC" in "Austin, TX"). It finds the companies, crawls their websites and job boards, maps their people by department and seniority, flags the likely hiring manager for each open role, and finds or guesses their emails. Everything lives in one local SQLite file you can browse in a web UI, export to CSV or push to Loxo.

## Quick start

```bash
pip install -e .            # Python 3.10+
zoomollo serve              # web UI at http://127.0.0.1:8080
```

Open the **Prospect** tab, type what the companies do and the city, and hit *Find companies*. Results fill in the **People** tab (filter to *Hiring managers*) and each company gets an org-map page.

### Turn on web search (recommended)

Without a search API, companies come only from OpenStreetMap and people only from company websites. With one, you get many more companies and people. Set one of these before `zoomollo serve`:

```bash
export BRAVE_API_KEY=...      # https://brave.com/search/api/ (cheapest; has a free allowance)
export SERPAPI_KEY=...        # https://serpapi.com/ (Google results)
export SEARXNG_URL=http://localhost:8888   # free, self-hosted SearXNG with JSON output enabled
```

A 25-company run uses roughly 100–130 searches (4 to find companies, up to 4 per company for people).

Or from the command line:

```bash
zoomollo prospect "commercial HVAC" "Austin, TX" --limit 25
zoomollo people --hiring                    # likely hiring managers
zoomollo export --people --hiring -o hiring-managers.csv
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

## How a prospect run works

1. **Find companies.** OpenStreetMap businesses inside the city's map boundary whose type or name matches your words (free, includes website, phone, address when listed). Plus web searches like "commercial HVAC companies in Austin, TX" and job-board searches. Directories (Yelp, BBB, Indeed, LinkedIn…) and "Top 10" list articles are filtered out.
2. **Crawl each website.** Team, leadership, about, contact and careers pages first. Names and titles come from team cards and structured data; emails and phones from the page. If the careers page links to Greenhouse, Lever, Ashby or Workable, all open roles are pulled.
3. **Map people.** Search-engine results for public profiles at the company ("Jane Doe - VP of Sales - Acme | LinkedIn"), read from the search results only. Only results that name the company are kept. Everyone is sorted into a department and a level (Exec, VP, Director, Manager, Staff).
4. **Hiring managers.** For each open role: the closest more-senior people in the same department, then a recruiter. Small companies with no matching department fall back to the owner/CEO.
5. **Emails.** Real addresses found on the site are matched to people. The company's email format is learned from them and used to guess the rest (marked `guessed`).

Direct phone numbers for individuals are only found when a company publishes them; otherwise you get the main line.

## Where the data comes from

| Source | How | What you get |
|---|---|---|
| OpenStreetMap | Overpass API, inside the city's bounding box | Company names, websites, phones, addresses |
| Web search | Brave / SerpAPI / SearXNG | More companies, and people from public profile results |
| Company websites | Crawler goes for contact, team, about, leadership and careers pages first. Reads `mailto:`/`tel:` links, page text, obfuscated addresses (`jane [at] acme [dot] com`) and schema.org `Person` data. | Emails, phones, names and titles, social profile links |
| Job boards | Official public JSON feeds for Greenhouse, Lever, Ashby and Workable | Open roles (hiring signals) plus recruiter emails and phones in job posts |
| Email format | Learned from named emails found on a company's site (e.g. `first.last`, 90% of examples) | Guessed addresses for any name at that company, labelled `guessed` |

Every contact is labelled by kind: `person` (seen with a name), `role` (info@, careers@…), `company` (main phone lines) or `guessed`, and stores the page it was found on.

## Loxo

Set `LOXO_AGENCY_SLUG` and `LOXO_API_KEY` (and `LOXO_DOMAIN` if you're not on `app.loxo.co`). `zoomollo loxo-push` sends mapped people who have a found email and haven't been pushed yet (add `--hiring` for hiring managers only, `--include-guessed` to include guessed emails). It's a dry run by default so you can check the payload; confirm the field names against your Loxo API docs before the first `--send`. CSV export also works with Loxo's importer.

## What it doesn't do (on purpose)

- **No LinkedIn, Facebook or Instagram scraping.** Their terms forbid it, they block it aggressively, and accounts get banned. People are found from search-engine results that link to public profiles; the profile pages themselves are never visited.
- **No CAPTCHA or login bypassing.** It identifies itself, follows `robots.txt` and waits between requests (1s per site by default).
- **No SMTP "pinging" to verify mailboxes.** Most cloud hosts block port 25 and it hurts your sending domain's reputation. Guesses only check that the domain accepts mail (MX record). Plug in a verifier such as NeverBounce or ZeroBounce for that.

## Compliance

You're responsible for how you use the data. Business contact data used for B2B outreach is generally allowed in the US (follow CAN-SPAM: honest headers and an unsubscribe). For people in the EU/UK (GDPR) or California (CCPA) you need a lawful basis, must tell people where you got their data, and must honour deletion requests. Use `zoomollo suppress` for opt-outs; suppressed emails, phones or whole `@domains` are deleted and never stored again.

## Ideas for next steps

- Google Places as an extra company source (paid, but very complete for local businesses).
- More job boards: SmartRecruiters, Recruitee, BambooHR, JazzHR, Indeed/LinkedIn via their partner APIs.
- Company discovery from lists (Crunchbase export, state business registries, conference sponsor pages).
- Email verification API, scheduled re-crawls, and Postgres for multi-user use.

## Development

```bash
pip install -e '.[dev]'
pytest
```
