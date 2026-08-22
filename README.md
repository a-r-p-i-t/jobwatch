# jobwatch

Polls company career boards directly and pings you on Telegram within ~10 minutes
of a new job going live — instead of waiting on LinkedIn's daily digest, which
only sees a posting 18–48 hours after it appears on the company's own board.

Stdlib Python only. Runs free on GitHub Actions. No server.

---

## Setup (~15 minutes, once)

### 1. Create the Telegram bot

1. Open Telegram, message **@BotFather**, send `/newbot`.
2. Pick a name and a username ending in `bot`.
3. He replies with a token like `8123456789:AAH...`. Keep it.
4. **Message your new bot** — send it anything. A bot cannot start a chat with
   you; if you skip this, every send will fail.
5. Get your chat ID: open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and look
   for `"chat":{"id":123456789`. That number is your chat ID.

### 2. Put the code on GitHub

Create a repo and push these four files:

```
jobwatch.py
companies.json
README.md
.github/workflows/jobwatch.yml
```

**Make the repo public.** Actions minutes are unlimited on public repos; private
repos get 2,000 minutes/month, which a 10-minute cron burns through in about
two weeks. Your token never goes in the code — it lives in Secrets — so a public
repo is safe here.

If you'd rather keep it private, change the cron to `*/30 * * * *`.

### 3. Add the secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the BotFather token |
| `TELEGRAM_CHAT_ID` | the number from `getUpdates` |

### 4. Check which sources actually work

Repo → **Actions → jobwatch → Run workflow**, set mode to `check`.

The log prints one line per company. Expect several `FAIL`s on the first run —
the slugs in `companies.json` are best guesses and need confirming (see
*Fixing sources* below). Fix what you can, set `"disabled": true` on the rest.

### 5. Seed, then go live

Run the workflow once with mode `seed`. This records every currently-open job
**without** notifying you — otherwise your first real run would fire off
hundreds of alerts for jobs that have been up for weeks.

After that, the cron takes over. You'll only hear about genuinely new postings.

---

## Fixing sources

`--check` failing on a company means the slug or endpoint is wrong. How to find
the right one:

**Greenhouse** — the board is `boards.greenhouse.io/<slug>` or
`job-boards.greenhouse.io/<slug>`. Whatever's in that URL is the slug.
Verify: `https://boards-api.greenhouse.io/v1/boards/<slug>/jobs` returns JSON.

**Lever** — board is `jobs.lever.co/<slug>`.
Verify: `https://api.lever.co/v0/postings/<slug>?mode=json`.

**Ashby** — board is `jobs.ashbyhq.com/<slug>`.

**SmartRecruiters** — board is `jobs.smartrecruiters.com/<Slug>`. Case-sensitive.

**Workday** — the careers URL looks like
`https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced`. Map it as:

```json
{ "host": "adobe.wd5.myworkdayjobs.com", "tenant": "adobe", "site": "external_experienced" }
```

The tenant is usually the subdomain, but not always — if it fails, open the
careers page, hit F12 → Network → filter `jobs`, and read the real path off the
POST request. It'll be `/wday/cxs/<tenant>/<site>/jobs`.

**Don't know which ATS a company uses?** Open their careers page and look at
where the "Apply" button sends you. The domain gives it away.

The companies still marked `"disabled": true` in the config (Microsoft, JPMorgan,
Citi, Barclays, NatWest, Amex, PayPal, Oracle, Flipkart, Myntra, Swiggy,
PhonePe, Paytm) use ATS platforms this script doesn't cover yet — mostly Phenom,
Eightfold, Oracle Recruiting, and in-house boards. Each needs its own adapter
function. They're maybe 15 lines each once you've read the network tab.

---

## Tuning the filters

In `companies.json`:

- **`location_include`** — a job passes if any of these strings appear in its
  location. Empty the list to stop filtering by location.
- **`title_exclude`** — whole-word match against the title. Anything matching is
  dropped.
- **`title_include`** — if non-empty, the title or location must contain at
  least one of these. Use it to narrow to specific roles, e.g.
  `["software engineer", "data analyst", "backend"]`.

⚠️ **Watch `"manager"`.** It's in the exclude list to filter out Engineering
Manager roles, but it also kills *Associate Product Manager* and *Management
Trainee* — both genuinely entry-level. Same story with `"lead"`, which drops
*Lead Generation Analyst*. If you want those, remove the word and instead
exclude the exact phrases you don't want.

Run mode `dry-run` after changing filters — it prints what it would send without
sending anything.

---

## Things worth knowing

- **GitHub's cron is approximate.** Scheduled runs get delayed under load,
  sometimes 15–20 minutes. Occasionally one is skipped entirely. Nothing is
  lost — the next run catches it, since state is diff-based, not time-based.
- **Scheduled workflows auto-disable after 60 days of repo inactivity.** The
  bot's own `seen.json` commits count as activity, so this shouldn't bite you.
- **If every source fails at once**, the script exits without touching state,
  so a network blip won't cause a flood of false "new" jobs on the next run.
- **`seen.json` prunes entries older than 60 days** to stop it growing forever.
- Polling every 10 minutes is well within normal traffic for these public
  endpoints. Don't drop it to every 30 seconds — you'll get rate-limited and
  gain nothing, since jobs don't post that fast.

---

## Local use

```bash
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...

python jobwatch.py --check      # test sources
python jobwatch.py --seed       # record current state, no alerts
python jobwatch.py --dry-run    # show what would be sent
python jobwatch.py              # for real
```
