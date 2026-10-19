"""Job alerts for Hyderabad roles at selected companies.

Sources:
  - Micron: public careers API (Eightfold)
  - Google: careers search page, newest first

Sends one Telegram message per company (or a GitHub issue if Telegram isn't set).
State is kept in seen_jobs.json, committed back to the repo by the workflow.
"""
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ---------------- Config (edit these) ----------------
LOCATION = "Hyderabad"
COMPANIES = ["Micron", "Google"]   # remove a name to stop tracking it
# A job matches if its title contains ANY of these (case-insensitive, whole word/phrase).
# Set to [] to get alerts for every Hyderabad posting.
KEYWORDS = [
    "data engineer", "data engineering", "data architect", "data platform",
    "analytics", "business intelligence", "bi", "power bi", "etl",
    "azure", "fabric", "databricks", "snowflake", "sql", "big data",
    "data scientist", "machine learning", "cloud",
]
EXCLUDE = ["intern", "internship"]   # titles containing these are skipped
MAX_AGE_DAYS = 14                    # Micron only (Google shows no post date). 0 = off
# Google searches (page 1, newest first). "" = all Hyderabad jobs.
GOOGLE_QUERIES = ["", "data", "analytics", "cloud"]
# -----------------------------------------------------

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_jobs.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def http(url, data=None, headers=None, as_json=True, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", "replace")
                return json.loads(body) if as_json else body
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"Retry {attempt + 1} for {url[:80]}: {e}")
            time.sleep(5 * (attempt + 1))


# ---------------- Sources ----------------
# Each returns a list of dicts: id, title, url, posted (epoch or None), extra (str)

def fetch_micron():
    api = "https://careers.micron.com/api/pcsx/search"
    raw, start = {}, 0
    for _ in range(100):
        q = urllib.parse.urlencode({"domain": "micron.com", "query": "", "location": LOCATION, "start": start})
        data = http(f"{api}?{q}", headers={"Accept": "application/json"}).get("data") or {}
        positions = data.get("positions") or []
        new = [p for p in positions if p["id"] not in raw]
        if not new:
            break
        for p in new:
            raw[p["id"]] = p
        start += len(positions)
        if start >= (data.get("count") or 0):
            break
        time.sleep(1)
    jobs = []
    for p in raw.values():
        loc = p.get("locations")
        loc = ", ".join(loc) if isinstance(loc, list) else str(loc or "")
        if LOCATION.lower() not in loc.lower():
            continue
        jobs.append({
            "id": f"micron:{p['id']}",
            "title": p["name"],
            "url": f"https://careers.micron.com/careers/job/{p['id']}",
            "posted": p.get("postedTs") or p.get("creationTs"),
            "extra": f"{loc} | {p.get('workLocationOption') or '-'} | {p.get('displayJobId', '')}",
        })
    return jobs


def fetch_google():
    base = "https://www.google.com/about/careers/applications/jobs/results/"
    jobs = {}
    for q in GOOGLE_QUERIES:
        params = {"location": f"{LOCATION}, Telangana, India", "sort_by": "date"}
        if q:
            params["q"] = q
        page = http(f"{base}?{urllib.parse.urlencode(params)}", as_json=False,
                    headers={"Accept-Language": "en-US,en;q=0.9"})
        ids = list(dict.fromkeys(re.findall(r"jobs/results/(\d+)-([a-z0-9-]+)", page)))
        titles = [html.unescape(re.sub(r"<[^>]+>", "", t)).strip()
                  for t in re.findall(r"<h3[^>]*>(.*?)</h3>", page, re.S)]
        titles = [t for t in titles if t]
        use_h3 = len(titles) == len(ids)
        for i, (jid, slug) in enumerate(ids):
            title = titles[i] if use_h3 else slug.replace("-", " ").title()
            jobs[jid] = {
                "id": f"google:{jid}",
                "title": title,
                "url": f"{base}{jid}-{slug}",
                "posted": None,
                "extra": f"{LOCATION}, Telangana, India",
            }
        time.sleep(2)
    return list(jobs.values())


SOURCES = {"Micron": fetch_micron, "Google": fetch_google}


# ---------------- Logic ----------------

def matches(title):
    t = title.lower()
    if any(x in t for x in EXCLUDE):
        return False
    return not KEYWORDS or any(re.search(rf"\b{re.escape(k)}\b", t) for k in KEYWORDS)


def fmt(job):
    date = (f" | Posted {datetime.fromtimestamp(job['posted'], timezone.utc):%d %b %Y}"
            if job.get("posted") else "")
    return f"{job['title']}\n{job['extra']}{date}\n{job['url']}"


def notify(title, body):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        text = f"{title}\n\n{body}" if body else title
        for i in range(0, len(text), 3900):  # Telegram limit is 4096 chars
            data = urllib.parse.urlencode({"chat_id": chat, "text": text[i:i + 3900],
                                           "disable_web_page_preview": "true"}).encode()
            http(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        return
    repo, gh = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if repo and gh:
        http(f"https://api.github.com/repos/{repo}/issues",
             data=json.dumps({"title": title, "body": (body or title)[:60000]}).encode(),
             headers={"Authorization": f"Bearer {gh}", "Content-Type": "application/json",
                      "Accept": "application/vnd.github+json"})
    else:
        print("No notifier configured.")


def load_seen():
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE) as f:
        ids = json.load(f)
    # Older versions stored bare Micron IDs; prefix them.
    return {i if ":" in str(i) else f"micron:{i}" for i in map(str, ids)}


def main():
    seen = load_seen()
    cutoff = time.time() - MAX_AGE_DAYS * 86400 if MAX_AGE_DAYS else 0
    failures = []

    for company in COMPANIES:
        try:
            jobs = SOURCES[company]()
        except Exception as e:
            print(f"{company}: fetch failed: {e}")
            failures.append(company)
            continue
        print(f"{company}: fetched {len(jobs)} {LOCATION} jobs")
        if not jobs:
            print(f"{company}: no jobs returned; site may have changed. Skipping.")
            failures.append(company)
            continue

        prefix = company.lower() + ":"
        first_run = not any(s.startswith(prefix) for s in seen)
        matching = [j for j in jobs if matches(j["title"])]
        new = [j for j in matching if j["id"] not in seen
               and (j["posted"] is None or j["posted"] >= cutoff)]
        new.sort(key=lambda j: j["posted"] or 0, reverse=True)

        if first_run and company != "Micron":
            # Baseline silently so you aren't flooded with existing openings.
            notify(f"Now tracking {company} {LOCATION}: {len(matching)} matching open role(s). "
                   f"You'll be alerted on new postings from here on.", "")
        elif new:
            notify(f"{company} {LOCATION}: {len(new)} new matching job(s)",
                   "\n\n".join(fmt(j) for j in new))
            print("\n\n".join(fmt(j) for j in new))
        else:
            print(f"{company}: no new matching jobs")

        seen |= {j["id"] for j in jobs}

    with open(STATE_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=0)

    if failures and len(failures) == len(COMPANIES):
        raise SystemExit(f"All sources failed: {failures}")


if __name__ == "__main__":
    main()
