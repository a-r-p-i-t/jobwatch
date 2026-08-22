#!/usr/bin/env python3
"""
jobwatch — poll company career boards directly and alert on new matching jobs.

Why this exists: LinkedIn indexes a posting 18-48h after it goes live on the
company's own board, and its alerts are daily/weekly only. Polling the source
gets you there first.

Usage:
    python jobwatch.py --check     # test every configured source, print results
    python jobwatch.py --seed      # record all current jobs WITHOUT notifying
    python jobwatch.py             # normal run: notify on anything new
    python jobwatch.py --dry-run   # print what would be sent, send nothing

Stdlib only. No pip install needed.
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "companies.json")
STATE_PATH = os.path.join(HERE, "seen.json")

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
TIMEOUT = 25
RETRIES = 2
STATE_TTL_DAYS = 60


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def http(url, method="GET", payload=None, headers=None):
    """Fetch a URL and parse JSON. Raises on failure after retries."""
    data = None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)

    last = None
    for attempt in range(RETRIES + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001 - we want to retry on anything
            last = exc
            if attempt < RETRIES:
                time.sleep(1.5 * (attempt + 1))
    raise last


# --------------------------------------------------------------------------
# Adapters — each returns a list of dicts:
#   {"uid", "title", "location", "url"}
# --------------------------------------------------------------------------

def fetch_greenhouse(src):
    slug = src["slug"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    out = []
    for j in http(url).get("jobs", []):
        out.append({
            "uid": f"greenhouse:{slug}:{j.get('id')}",
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
        })
    return out


def fetch_lever(src):
    slug = src["slug"]
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    out = []
    for j in http(url):
        cats = j.get("categories") or {}
        out.append({
            "uid": f"lever:{slug}:{j.get('id')}",
            "title": j.get("text", ""),
            "location": cats.get("location", "") or "",
            "url": j.get("hostedUrl", ""),
        })
    return out


def fetch_ashby(src):
    slug = src["slug"]
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    out = []
    for j in http(url).get("jobs", []):
        out.append({
            "uid": f"ashby:{slug}:{j.get('id')}",
            "title": j.get("title", ""),
            "location": j.get("location", "") or "",
            "url": j.get("jobUrl", ""),
        })
    return out


def fetch_smartrecruiters(src):
    slug = src["slug"]
    out = []
    offset = 0
    while True:
        url = (f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
               f"?limit=100&offset={offset}")
        data = http(url)
        items = data.get("content", [])
        for j in items:
            loc = j.get("location") or {}
            where = ", ".join(x for x in [loc.get("city"), loc.get("region"),
                                          loc.get("country")] if x)
            out.append({
                "uid": f"smartrecruiters:{slug}:{j.get('id')}",
                "title": j.get("name", ""),
                "location": where,
                "url": f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
            })
        offset += len(items)
        if len(items) < 100 or offset >= data.get("totalFound", 0):
            break
    return out


def fetch_workday(src):
    """Workday tenants expose a JSON search under /wday/cxs/{tenant}/{site}/jobs.

    Workday does NOT reliably sort newest-first, so we must page through
    everything rather than reading the first N and stopping — otherwise a job
    posted today could sit at position 900 and never be seen.
    """
    host, tenant, site = src["host"], src["tenant"], src["site"]
    endpoint = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    ceiling = src.get("max_jobs", 2000)
    out = []
    offset = 0
    total = None
    while offset < ceiling:
        body = {"appliedFacets": src.get("facets", {}),
                "limit": 20, "offset": offset,
                "searchText": src.get("searchText", "")}
        data = http(endpoint, method="POST", payload=body)
        if total is None:
            total = data.get("total")
        posts = data.get("jobPostings", [])
        for j in posts:
            path = j.get("externalPath", "")
            out.append({
                "uid": f"workday:{tenant}:{path}",
                "title": j.get("title", ""),
                "location": j.get("locationsText", "") or "",
                "url": f"https://{host}/{site}{path}",
            })
        offset += len(posts)
        if len(posts) < 20 or (total and offset >= total):
            break
    if total and len(out) < total:
        print(f"     (truncated at {len(out)} of {total} — raise max_jobs)")
    return out


def fetch_phenom(src):
    """Phenom People careers sites (careers.<company>.com) expose /api/apply/v2/jobs."""
    host, domain = src["host"], src["domain"]
    out = []
    start = 0
    while start < src.get("max_jobs", 2000):
        url = (f"https://{host}/api/apply/v2/jobs?domain={domain}"
               f"&start={start}&num=100&profileData=false")
        data = http(url)
        # Phenom returns either {"jobs":[...]} or {"refineSearch":{"data":{"jobs":[...]}}}
        jobs = data.get("jobs")
        if jobs is None:
            jobs = (((data.get("refineSearch") or {}).get("data") or {})
                    .get("jobs") or [])
        for j in jobs:
            where = j.get("cityStateCountry") or ", ".join(
                x for x in [j.get("city"), j.get("state"), j.get("country")] if x)
            out.append({
                "uid": f"phenom:{domain}:{j.get('jobId')}",
                "title": j.get("title", ""),
                "location": where or "",
                "url": j.get("applyUrl") or j.get("jobSeoUrl") or "",
            })
        start += len(jobs)
        if len(jobs) < 100:
            break
    return out


def fetch_amazon(src):
    params = {
        "normalized_country_code[]": src.get("country", "IND"),
        "result_limit": "100",
        "sort": "recent",
        "offset": "0",
    }
    url = "https://www.amazon.jobs/en/search.json?" + urllib.parse.urlencode(params)
    out = []
    for j in http(url).get("jobs", []):
        out.append({
            "uid": f"amazon:{j.get('id_icims')}",
            "title": j.get("title", ""),
            "location": j.get("location", "") or "",
            "url": "https://www.amazon.jobs" + (j.get("job_path") or ""),
        })
    return out


ADAPTERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
    "amazon": fetch_amazon,
    "phenom": fetch_phenom,
}


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------

def matches(job, filters):
    title = (job.get("title") or "").lower()
    loc = (job.get("location") or "").lower()
    blob = f"{title} {loc}"

    inc_loc = [s.lower() for s in filters.get("location_include", [])]
    if inc_loc and not any(s in loc for s in inc_loc):
        return False

    for bad in filters.get("title_exclude", []):
        if re.search(r"\b" + re.escape(bad.lower()) + r"\b", title):
            return False

    inc_title = [s.lower() for s in filters.get("title_include", [])]
    if inc_title and not any(s in blob for s in inc_title):
        return False

    return True


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh).get("seen", {})
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(seen):
    cutoff = datetime.now(timezone.utc) - timedelta(days=STATE_TTL_DAYS)
    pruned = {}
    for uid, ts in seen.items():
        try:
            if datetime.fromisoformat(ts) >= cutoff:
                pruned[uid] = ts
        except ValueError:
            pruned[uid] = ts
    payload = {"updated": datetime.now(timezone.utc).isoformat(), "seen": pruned}
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
    return len(pruned)


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def telegram_send(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("!! TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set", file=sys.stderr)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = {"chat_id": chat, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True}
    try:
        http(url, method="POST", payload=body)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"!! telegram send failed: {exc}", file=sys.stderr)
        return False


def format_job(company, job):
    title = html.escape(job["title"])
    loc = html.escape(job.get("location") or "—")
    url = job.get("url") or ""
    line = f"<b>{html.escape(company)}</b> — {title}\n<i>{loc}</i>"
    if url:
        line += f'\n<a href="{html.escape(url, quote=True)}">Apply</a>'
    return line


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def collect(config, verbose=False):
    """Fetch every source. Returns (results, errors)."""
    results, errors = [], []
    for src in config["sources"]:
        if src.get("disabled"):
            continue
        company = src.get("company", src.get("slug", "?"))
        fn = ADAPTERS.get(src.get("type"))
        if not fn:
            errors.append((company, f"unknown type {src.get('type')!r}"))
            continue
        try:
            jobs = fn(src)
            for j in jobs:
                j["company"] = company
            results.extend(jobs)
            if verbose:
                kept = sum(1 for j in jobs if matches(j, config["filters"]))
                tag = "OK  " if jobs else "ZERO"
                print(f"  {tag} {company:<22} {len(jobs):>4} jobs, {kept:>3} match filters")
                if not jobs:
                    print("       ^ responded but returned nothing — slug is probably wrong")
        except Exception as exc:  # noqa: BLE001
            errors.append((company, str(exc)[:160]))
            if verbose:
                print(f"  FAIL {company:<22} {str(exc)[:90]}")
    return results, errors


def cmd_check(config):
    print("Testing every configured source...\n")
    _, errors = collect(config, verbose=True)
    print()
    if errors:
        print(f"{len(errors)} source(s) failed — fix or set \"disabled\": true on them:")
        for company, err in errors:
            print(f"  - {company}: {err}")
    else:
        print("All sources responded.")
    return 0 if not errors else 1


def cmd_run(config, seed=False, dry_run=False):
    seen = load_state()
    first_ever = not seen
    jobs, errors = collect(config)

    if not jobs and errors:
        print("!! every source failed; leaving state untouched", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc).isoformat()
    fresh = []
    for j in jobs:
        if j["uid"] in seen:
            continue
        seen[j["uid"]] = now
        if matches(j, config["filters"]):
            fresh.append(j)

    if seed or first_ever:
        kept = save_state(seen)
        print(f"Seeded {kept} job IDs from {len(jobs)} listings. "
              f"No alerts sent. Future runs will notify on new postings only.")
        return 0

    print(f"{len(jobs)} listings scanned, {len(fresh)} new match(es), "
          f"{len(errors)} source error(s)")

    if fresh:
        fresh.sort(key=lambda j: (j["company"], j["title"]))
        chunks = [fresh[i:i + 6] for i in range(0, len(fresh), 6)]
        for idx, chunk in enumerate(chunks):
            header = f"🔔 <b>{len(fresh)} new job(s)</b>"
            if len(chunks) > 1:
                header += f"  ({idx + 1}/{len(chunks)})"
            body = header + "\n\n" + "\n\n".join(
                format_job(j["company"], j) for j in chunk)
            if dry_run:
                print("\n--- would send ---\n" + body)
            else:
                telegram_send(body)
                time.sleep(1)

    if not dry_run:
        save_state(seen)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="test sources, send nothing")
    ap.add_argument("--seed", action="store_true", help="record current jobs, no alerts")
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending")
    args = ap.parse_args()

    with open(CONFIG_PATH, encoding="utf-8") as fh:
        config = json.load(fh)
    config.setdefault("filters", {})

    if args.check:
        return cmd_check(config)
    return cmd_run(config, seed=args.seed, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
