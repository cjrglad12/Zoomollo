# Zoomollo

Self-hosted contact discovery: a do-it-yourself ZoomInfo/Apollo with no search engines, data vendors or paid services. Tell it what kind of companies and which city ("commercial HVAC" in "Austin, TX"). It finds the companies, crawls their websites and job boards, maps their people by department and seniority, flags the likely hiring manager for each open role, and finds or guesses their emails. Everything lives in one local file on your computer that you can browse in a web page, export to CSV or push to Loxo.

## Quick start

1. **Download:** on GitHub, Code → Download ZIP, then unzip it.
2. **Start it:** double-click **`start.command`** (Mac) or **`start.bat`** (Windows). The first run installs everything (a minute or two) and opens Zoomollo in your browser. If Python 3.10+ is missing, it opens the download page; install it (on Windows tick "Add python.exe to PATH") and double-click again.
   - Mac says it can't open the file? Right-click it → Open → Open. (On newer macOS: System Settings → Privacy & Security → Open Anyway.)
3. **Prospect:** on the **Prospect** tab, type what the companies do and the city, and hit *Find companies*. Results fill the **People** tab (tick *Hiring managers*), the **Jobs** tab, and a page per company with its org map.

No keys or accounts are needed. Keep the black Zoomollo window open while you use it; close it to stop. Your data stays in `zoomollo.db` in the same folder.

The **Theme** (Light / Auto / Dark) and **Layout** (PC / Auto / Mobile) switches sit at the top of every page. *Auto* follows your device; your choice is remembered in that browser.

Prefer the command line? `pip install -e .` then `zoomollo serve --open`.

```bash
zoomollo prospect "commercial HVAC" "Austin, TX" --limit 25
zoomollo people --hiring                    # likely hiring managers
zoomollo export --people --hiring -o hiring-managers.csv
zoomollo hiring "technician"                # open roles with their matched contacts
zoomollo export --jobs -o roles-contacts.csv
zoomollo sources add https://data.yourcity.gov/resource/xxxx-xxxx.json --label "City permits"
zoomollo sources test licenses.csv "hvac" "Austin, TX"   # preview what a data file gives
zoomollo email-bad bob.smith@acme.com       # wrong/bounced -> next guess (up to 10 tries)
zoomollo email-good bob@acme.com            # correct -> locks in the company's email format
zoomollo bounces bounces.txt                # paste/export of bounce notices or a list of addresses
zoomollo crawl acme.com globex.io           # crawl specific sites (or: zoomollo crawl @domains.txt)
zoomollo jobs greenhouse:stripe lever:plaid # a company's job board directly
zoomollo guess "Jane Doe" acme.com          # best-guess work email
zoomollo search "vp sales"                  # search saved contacts
zoomollo suppress jane@acme.com             # opt-out: delete + never store again
zoomollo block staffco.com                  # block a staffing/recruiting firm the filters missed
zoomollo text 12 --template 1               # WhatsApp link with a filled-in template for person #12
zoomollo loxo-push                          # dry run; add --send to push for real
```

## How a prospect run finds companies

No search engines or paid services. Zoomollo reads public sources directly with its own crawler:

1. **Map data.** Businesses inside the city's map boundary whose type or name matches your words, from OpenStreetMap's free public servers (often with website, phone and address).
2. **Chambers of commerce and business associations** in the city (also found on the map). Their member directories are crawled, industry categories first, and each member website is checked: its homepage has to mention the industry.
3. **Your public data sources** (see below): license lists, permit records, business registrations.
4. **Links between companies.** Partner, supplier and client links on the company websites it crawls. A linked site is kept only if its homepage mentions the industry and the city or state.
5. **Website guessing.** For businesses listed without a website, likely addresses (name.com, name.net…) are tried. One is accepted only if it shows the business's phone number, or its name plus the city/state.

General words such as *commercial*, *residential* or *local* don't count as a match on their own. Directories (Yelp, BBB, Indeed, LinkedIn…) and staffing/recruiting firms are always skipped.

### Public data sources

Government websites publish lists that are great for finding companies: state contractor and professional license lists, city building permits (contractor name and phone), business registrations. Most offer a **CSV download** or an open-data **JSON link** (often `data.<city>.gov/resource/xxxx-xxxx.json`).

Add the link (or a downloaded file's path) under **Public data sources** on the Prospect tab, or with `zoomollo sources add`. Every prospect run reads each source and keeps rows that mention the industry and, where there's a city column, the city. Columns are recognised by name: business/contractor name, phone, email, website, address, city, state, license type, owner. Owners or license holders become people at the company.

## Then, for each company

1. **Crawl the website.** Team, leadership, about, contact and careers pages first, plus the sitemap. Names and titles come from team cards and structured data; emails and phones from the page.
2. **Open roles.** From the company's careers page (job data most career sites publish for Google Jobs, and links that read like job titles) and its Greenhouse, Lever, Ashby or Workable job board when the careers page links to one.
3. **Map people.** Everyone found is sorted into a department and a level (Exec, VP, Director, Manager, Staff).
4. **Roles × contacts.** Every open role is cross-referenced with the company's people, strongest evidence first:

   | Match | Meaning |
   |---|---|
   | named in posting | the person's name, email or phone appears in the job post |
   | reports to | the post says "reports to the Service Manager" and that person was found |
   | contact in posting | an email/phone in the post not tied to a known person |
   | likely hiring manager | closest more-senior person in the same department |
   | owner / exec | small company with no matching department leader |
5. **Emails.** Real addresses found on the site or in job posts are matched to people. The company's email format is learned from them and used to guess the rest (marked `guessed`).

People come from company websites, job posts and public records only. Direct phone numbers for individuals are found only when a company publishes them; otherwise you get the main line.

## Fixing wrong email guesses

Each person's email shows its status: `found`, `guessed (try 3/10)`, `confirmed` or `failed`.

- **✗ (or `zoomollo email-bad`)**: the address is blocked forever, its format counts against that company, and the person gets the next most likely guess. Formats that bounced at a company go to the back of the line for everyone there. Nickname variants (Robert → Bob, Rob) are tried after the formal name.
- **✓ (or `zoomollo email-good`)**: the person is `confirmed`, that format becomes the company's format, and colleagues who are still on a guess are switched to it.
- **Bounces in bulk**: paste bounce notices or a list into *Bounced emails* on the Prospect tab, or run `zoomollo bounces file.txt`. Only addresses already saved are acted on.
- **After 10 wrong guesses** (or when no guesses are left) the person is marked `failed`.
- A guess that later appears in a job post or on the site is upgraded to `found` automatically.

## Texting (WhatsApp / SMS) and templates

The **Messages** tab, and the 💬 button next to any phone number, prepare a text for you:

- **Templates** with fields filled in per person: `{first_name}`, `{last_name}`, `{name}`, `{title}`, `{company}`, `{city}`, `{role}` (the open role they most likely own), `{my_name}`, `{my_company}`. Three starter templates are included; edit them or add your own. Unknown fields are filled generically ("Hi there", "a new team member") and flagged so you can tweak the text.
- **Draft with AI** on your own computer: install [Ollama](https://ollama.com) (free), run `ollama pull llama3.2` once, and the *Draft* button writes a template from a goal and tone. Nothing is sent anywhere. Another model: set `OLLAMA_MODEL` in `zoomollo-settings.txt`.
- **One tap to send.** *WhatsApp* opens WhatsApp's official click-to-chat link with the message written; *Text* opens your phone's messaging app. You review and press Send. Zoomollo never sends on its own: automated WhatsApp sending needs a Meta business account and opted-in contacts, and unofficial automation gets numbers banned.
- **Outreach queue:** pick a template and step through hiring managers with their own phone number who haven't been texted yet.
- Every opened message is logged under *Sent before*. **They opted out (STOP)** blocks that number for good.

Phone numbers are often a company's main line, which may not be on WhatsApp; use *Text* or call instead. US texting rules (TCPA) are strict about automated texts to mobile numbers; sending one by one from your own phone and honouring STOP keeps you on the safer side.

## Always excluded

Rules live in `zoomollo/exclusions.py`; edit the lists there to change them.

- **Generic inboxes, ever.** info@, careers@, jobs@, hr@, sales@, hello@, office@, support@ and ~150 similar (including variants like `jobs.austin@` or `info2@`). They're never saved, never matched to a role, never guessed, and never pushed to Loxo.
- **Recruiting and staffing people.** Recruiters, talent acquisition, sourcers, staffing titles. They're never saved (and removed if a title is learned later) and never suggested for a role. HR managers/directors are kept in the org map but are not matched to roles unless the job is in HR.
- **Recruiting and staffing companies.** Agencies, executive search and temp firms are dropped during discovery (map tags, names, homepage text, a list of ~70 big firms' domains). If one gets through, its homepage gives it away during the crawl: the company and everything saved for it is deleted and its domain is blocked for good. Use the *Block* button on a company page or `zoomollo block` for any the filters miss.
- Existing databases are cleaned automatically the next time they're opened.

## What Zoomollo connects to

- The websites it crawls (company sites, chamber/association sites, and the government data links you add).
- OpenStreetMap's free public servers, for map lookups.
- Companies' own job boards (Greenhouse, Lever, Ashby, Workable) when their careers page links to one.
- WhatsApp or your phone's text app, only when you tap a send button (you press Send there).
- Ollama on your own computer, only if you install it for AI drafting.
- Loxo, only if you set it up (below).

No search engines, data vendors, email-verification services or other paid services.

## Loxo

Put `LOXO_AGENCY_SLUG` and `LOXO_API_KEY` in `zoomollo-settings.txt` (and `LOXO_DOMAIN` if you're not on `app.loxo.co`). `zoomollo loxo-push` sends mapped people who have a found email and haven't been pushed yet (add `--hiring` for hiring managers only, `--include-guessed` to include guessed emails). It's a dry run by default so you can check the payload; confirm the field names against your Loxo API docs before the first `--send`. CSV export also works with Loxo's importer.

## What it doesn't do (on purpose)

- **No LinkedIn, Facebook or Instagram scraping.** Their terms forbid it, they block it aggressively, and accounts get banned.
- **No CAPTCHA or login bypassing.** It identifies itself, follows `robots.txt` and waits between requests (1s per site by default).
- **No SMTP "pinging" to test mailboxes.** It gets your IP and domain blocklisted. Guesses only check that the domain accepts mail; your ✓/✗ feedback and bounces do the rest.

## Compliance

You're responsible for how you use the data. Business contact data used for B2B outreach is generally allowed in the US (follow CAN-SPAM: honest headers and an unsubscribe). For people in the EU/UK (GDPR) or California (CCPA) you need a lawful basis, must tell people where you got their data, and must honour deletion requests. Use `zoomollo suppress` for opt-outs; suppressed emails, phones or whole `@domains` are deleted and never stored again.

## Development

```bash
pip install -e '.[dev]'
pytest
```
