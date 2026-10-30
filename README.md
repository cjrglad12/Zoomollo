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
zoomollo hiring "technician"                # open roles with their matched contacts
zoomollo export --jobs -o roles-contacts.csv
zoomollo email-bad bob.smith@acme.com       # wrong/bounced -> next guess (up to 10 tries)
zoomollo email-good bob@acme.com            # correct -> locks in the company's email format
zoomollo bounces bounces.txt                # paste/export of bounce notices or a list of addresses
zoomollo verify --hiring                    # auto-check guesses (needs NEVERBOUNCE/ZEROBOUNCE key)
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
4. **Open roles.** From Greenhouse/Lever/Ashby/Workable, and straight from the company's own careers page (schema.org `JobPosting` data, which most career sites publish for Google Jobs, plus links that read like job titles). Sitemaps are read too, so bio and job pages that aren't in the menu still get found.
5. **Roles × contacts.** Every open role is cross-referenced with the company's people and contacts, strongest evidence first:

   | Match | Meaning |
   |---|---|
   | named in posting | the person's name, email or phone appears in the job post |
   | reports to | the post says "reports to the Service Manager" and that person was found |
   | contact in posting | an email/phone in the post not tied to a known person |
   | likely hiring manager | closest more-senior person in the same department |
   | owner / exec | small company with no matching department leader |
   | recruiter | HR / talent people |
   | careers inbox | jobs@, careers@, hr@ style addresses |
6. **Emails.** Real addresses found on the site or in job posts are matched to people. The company's email format is learned from them and used to guess the rest (marked `guessed`).

## Fixing wrong email guesses

Each person's email shows its status: `found`, `guessed (try 3/10)`, `confirmed`, `catch_all` or `failed`.

- **✗ (or `zoomollo email-bad`)**: the address is blocked forever, its format counts against that company, and the person gets the next most likely guess. Formats that bounced at a company go to the back of the line for everyone there. Nickname variants (Robert → Bob, Rob) are tried after the formal name.
- **✓ (or `zoomollo email-good`)**: the person is `confirmed`, that format becomes the company's format, and colleagues who are still on a guess are switched to it.
- **Bounces in bulk**: paste bounce notices or a list into *Bounced emails* on the Prospect tab, or run `zoomollo bounces file.txt`. Only addresses already saved are acted on.
- **After 10 wrong guesses** (or when no guesses are left) the person is marked `failed`.
- **Automatic**: set `NEVERBOUNCE_API_KEY` or `ZEROBOUNCE_API_KEY` and use the *check* button or `zoomollo verify`. It checks the guess, moves to the next one on "invalid", and stops at "valid", at 10 tries, or when the company's mail server accepts every address ("catch-all": no check can tell, so the most likely guess is kept).
- A guess that later appears in a job post or on the site is upgraded to `found` automatically.

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
