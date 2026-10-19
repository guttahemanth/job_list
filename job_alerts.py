"""Micron Hyderabad job alerts.

Polls Micron's public careers API (Eightfold), filters Hyderabad roles by keyword,
and sends new postings to Telegram (if configured) or opens a GitHub issue.
State is kept in seen_jobs.json, committed back to the repo by the workflow.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ---------------- Config (edit these) ----------------
LOCATION = "Hyderabad"
# A job matches if its title contains ANY of these (case-insensitive, whole word/phrase).
# Set to [] to get alerts for every Hyderabad posting.
KEYWORDS = [
    "data engineer", "data engineering", "data architect", "data platform",
    "analytics", "business intelligence", "bi", "power bi", "etl",
    "azure", "fabric", "databricks", "snowflake", "sql", "big data",
    "data scientist", "machine learning", "cloud",
]
# Titles containing any of these are skipped.
EXCLUDE = ["intern", "internship"]
# -----------------------------------------------------

API = "https://careers.micron.com/api/pcsx/search"
JOB_URL = "https://careers.micron.com/careers/job/{id}"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_jobs.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (job-alerts)", "Accept": "application/json"}


def get_json(url, data=None, headers=None, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers={**HEADERS, **(headers or {})})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"Retry {attempt + 1} after error: {e}")
            time.sleep(5 * (attempt + 1))


def fetch_jobs():
    jobs, start = {}, 0
    for _ in range(100):  # hard cap on pages
        q = urllib.parse.urlencode({"domain": "micron.com", "query": "", "location": LOCATION, "start": start})
        data = get_json(f"{API}?{q}").get("data") or {}
        positions = data.get("positions") or []
        new = [p for p in positions if p["id"] not in jobs]
        if not new:
            break
        for p in new:
            jobs[p["id"]] = p
        start += len(positions)
        if start >= (data.get("count") or 0):
            break
        time.sleep(1)
    return [j for j in jobs.values() if LOCATION.lower() in str(j.get("locations", "")).lower()]


def matches(title):
    t = title.lower()
    if any(x in t for x in EXCLUDE):
        return False
    if not KEYWORDS:
        return True
    return any(re.search(rf"\b{re.escape(k)}\b", t) for k in KEYWORDS)


def fmt(job):
    posted = job.get("postedTs") or job.get("creationTs")
    date = datetime.fromtimestamp(posted, timezone.utc).strftime("%d %b %Y") if posted else "-"
    loc = job.get("locations")
    loc = ", ".join(loc) if isinstance(loc, list) else loc
    return (f"{job['name']}\n{loc} | {job.get('workLocationOption') or '-'} | Posted {date} | "
            f"{job.get('displayJobId', '')}\n{JOB_URL.format(id=job['id'])}")


def send_telegram(text, token, chat_id):
    for i in range(0, len(text), 3900):  # Telegram limit is 4096 chars
        body = urllib.parse.urlencode({"chat_id": chat_id, "text": text[i:i + 3900],
                                       "disable_web_page_preview": "true"}).encode()
        get_json(f"https://api.telegram.org/bot{token}/sendMessage", data=body)


def open_github_issue(title, body):
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not (repo and token):
        print("No notifier configured; printing only.")
        return
    get_json(f"https://api.github.com/repos/{repo}/issues",
             data=json.dumps({"title": title, "body": body[:60000]}).encode(),
             headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                      "Accept": "application/vnd.github+json"})


def main():
    seen = set()
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            seen = set(json.load(f))

    jobs = fetch_jobs()
    print(f"Fetched {len(jobs)} {LOCATION} jobs")
    if not jobs:
        print("No jobs returned; API may have changed. Exiting without updating state.")
        sys.exit(1)

    new = [j for j in jobs if str(j["id"]) not in seen and matches(j["name"])]
    new.sort(key=lambda j: j.get("postedTs") or 0, reverse=True)
    print(f"New matching jobs: {len(new)}")

    if new:
        header = f"Micron {LOCATION}: {len(new)} new matching job(s)"
        body = "\n\n".join(fmt(j) for j in new)
        token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if token and chat:
            send_telegram(f"{header}\n\n{body}", token, chat)
        else:
            open_github_issue(header, body)
        print(body)

    # Remember every fetched job (matching or not) so keyword edits don't re-alert old posts.
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(seen | {str(j["id"]) for j in jobs}), f, indent=0)


if __name__ == "__main__":
    main()
