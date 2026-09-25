import json
import os
import re
import smtplib
import time
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path

import requests

from job_matcher import (
    locally_filter_jobs,
    score_jobs_batch,
)


# ============================================================
# CONFIGURATION
# ============================================================

# API Credentials & Settings
APIFY_TOKEN = os.environ.get("APIFY_API_TOKEN", "")
BRIGHT_DATA_API_KEY = os.environ.get("BRIGHT_DATA_API_KEY", "")
BRIGHT_DATA_DATASET_ID = os.environ.get(
    "BRIGHT_DATA_DATASET_ID",
    "gd_l1viktl72bvl7bjuj0",
)

GMAIL_USERNAME = os.environ.get("GMAIL_USERNAME", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

# Apify Actor Configuration
ACTOR_ID = "automation-lab~linkedin-jobs-scraper"
APIFY_URL = (
    f"https://api.apify.com/v2/acts/"
    f"{ACTOR_ID}/run-sync-get-dataset-items"
)

# Freshness Configuration:
# Hourly execution targets jobs posted since the last hourly run.
# A 90-minute window provides an intentional 30-minute safety buffer
# for clock drift, scraper delays, and LinkedIn indexing delays,
# without allowing stale jobs (e.g. 4+ hours old) to pass through.
FRESHNESS_WINDOW_MINUTES = int(
    os.environ.get("FRESHNESS_WINDOW_MINUTES", "90")
)

# Limits
MAX_EMAIL_JOBS = 20
MAX_SCRAPED_JOBS = 50
MIN_MATCH_SCORE = 75

# Persistent state management
BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
STATE_FILE = STATE_DIR / "seen_jobs.json"
METRICS_FILE = STATE_DIR / "last_run_metrics.json"

# Prevent the state file from growing forever
MAX_SEEN_JOBS = 5000

# Target Locations for Filter Validation
TARGET_LOCATIONS = [
    "india",
    "hyderabad",
    "bengaluru",
    "bangalore",
    "mumbai",
    "remote",
]


# ============================================================
# TIMEZONE & FORMATTING HELPERS
# ============================================================

IST_TIMEZONE = timezone(timedelta(hours=5, minutes=30))


def format_ist_and_utc(dt: datetime | None) -> str:
    """
    Format a datetime for email and logs in both IST and UTC.
    Example: '09:47 AM IST (04:17 UTC)'
    """
    if dt is None:
        return "Unknown"
    ist = dt.astimezone(IST_TIMEZONE)
    return (
        f"{ist.strftime('%I:%M %p IST')} "
        f"({dt.strftime('%H:%M UTC')})"
    )


# ============================================================
# STATE MANAGEMENT
# ============================================================

def load_state() -> dict:
    """
    Load persistent job state from disk.
    GitHub Actions restores agent/state from cache before running.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if not STATE_FILE.exists():
        return {
            "jobs": {},
            "last_run": {},
        }

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            state = json.load(file)

        if not isinstance(state, dict):
            return {"jobs": {}, "last_run": {}}

        if "jobs" not in state or not isinstance(state["jobs"], dict):
            state["jobs"] = {}

        if "last_run" not in state or not isinstance(state["last_run"], dict):
            state["last_run"] = {}

        return state

    except Exception as error:
        print(f"WARNING: Could not load state file: {error}")
        return {"jobs": {}, "last_run": {}}


def save_state(state: dict):
    """
    Save persistent job state and metrics to disk.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    jobs = state.get("jobs", {})

    # Keep only the newest MAX_SEEN_JOBS records
    if len(jobs) > MAX_SEEN_JOBS:
        sorted_items = sorted(
            jobs.items(),
            key=lambda item: item[1].get("last_seen", ""),
            reverse=True,
        )
        state["jobs"] = dict(sorted_items[:MAX_SEEN_JOBS])

    try:
        with STATE_FILE.open("w", encoding="utf-8") as file:
            json.dump(state, file, indent=2)

        if "last_run" in state and state["last_run"]:
            with METRICS_FILE.open("w", encoding="utf-8") as file:
                json.dump(state["last_run"], file, indent=2)

        print(
            f"State saved: {len(state['jobs'])} jobs tracked."
        )
    except Exception as error:
        print(f"ERROR: Could not save state: {error}")


# ============================================================
# STABLE IDENTIFIERS & CANONICAL URLS
# ============================================================

def extract_linkedin_job_id(job: dict) -> str | None:
    """
    Extract canonical numeric LinkedIn Job ID from job data or URL.
    """
    # 1. Direct fields
    for field in ("jobId", "id", "job_id"):
        val = job.get(field)
        if val is not None:
            val_str = str(val).strip()
            if val_str.isdigit():
                return val_str

    # 2. Extract from URL fields
    for field in ("jobUrl", "url", "applyUrl", "link"):
        url = str(job.get(field) or "").strip()
        if not url:
            continue

        match = (
            re.search(r"/jobs/view/(\d+)", url)
            or re.search(r"currentJobId=(\d+)", url)
            or re.search(r"jobId=(\d+)", url)
        )
        if match:
            return match.group(1)

    return None


def get_job_id(job: dict) -> str:
    """
    Generate a stable, unique identifier for a job.
    Preference:
    1. LinkedIn numeric Job ID
    2. Canonical URL
    3. Normalized title|company|location
    """
    lid = extract_linkedin_job_id(job)
    if lid:
        return lid

    raw_url = str(job.get("jobUrl") or job.get("url") or "").strip()
    if raw_url:
        clean_url = raw_url.split("?")[0].rstrip("/")
        if clean_url:
            return clean_url

    title = str(job.get("title") or "").strip().lower()
    company = str(
        job.get("companyName") or job.get("company") or ""
    ).strip().lower()
    location = str(job.get("location") or "").strip().lower()

    return f"{title}|{company}|{location}"


def get_canonical_job_url(job: dict, job_id: str) -> str:
    """
    Return clean canonical LinkedIn URL without tracking tokens.
    """
    if job_id and job_id.isdigit():
        return f"https://www.linkedin.com/jobs/view/{job_id}"

    raw_url = (
        job.get("jobUrl")
        or job.get("url")
        or job.get("applyUrl")
        or ""
    )
    if raw_url and "?" in raw_url:
        return raw_url.split("?")[0]
    return raw_url or "No URL available"


# ============================================================
# POSTING DATE & FRESHNESS PIPELINE
# ============================================================

def parse_posted_time(
    job: dict,
    reference_time: datetime | None = None,
) -> datetime | None:
    """
    Convert the job's posting timestamp into a timezone-aware UTC datetime.

    Supports:
    - ISO 8601 timestamps (e.g. '2026-09-24T04:17:00Z')
    - Unix epoch timestamps (seconds or milliseconds)
    - Relative LinkedIn timestamps ('13 minutes ago', '1 hour ago', '2 days ago')
    - Immediate keywords ('just now', 'just posted', 'today')
    """
    if reference_time is None:
        reference_time = datetime.now(timezone.utc)

    raw = (
        job.get("postedAt")
        or job.get("postedDate")
        or job.get("postedAtText")
        or job.get("date")
        or job.get("date_text")
        or job.get("time")
        or ""
    )

    if not raw:
        return None

    # Handle numeric epoch timestamp
    if isinstance(raw, (int, float)):
        try:
            # Check if milliseconds or seconds
            ts = float(raw)
            if ts > 1e11:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            pass

    raw_str = str(raw).strip()
    if not raw_str:
        return None

    # 1. ISO 8601 strings
    try:
        normalized = raw_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass

    text = raw_str.lower()

    # 2. Immediate keywords
    if any(
        kw in text
        for kw in ("just now", "just posted", "moments ago", "recently")
    ):
        return reference_time

    # 3. Relative regex matching
    match = re.search(
        r"(\d+)\s*(s|sec|second|seconds|m|min|minute|minutes|h|hr|hour|hours|d|day|days|w|week|weeks)\b",
        text,
    )
    if match:
        value = int(match.group(1))
        unit = match.group(2)

        if unit.startswith("s"):
            return reference_time - timedelta(seconds=value)
        if unit.startswith("m") and not unit.startswith("mo"):
            return reference_time - timedelta(minutes=value)
        if unit.startswith("h"):
            return reference_time - timedelta(hours=value)
        if unit.startswith("d"):
            return reference_time - timedelta(days=value)
        if unit.startswith("w"):
            return reference_time - timedelta(weeks=value)

    if "today" in text:
        # 'Today' without a time could be anywhere in the last 24h.
        return reference_time - timedelta(hours=6)

    return None


def calculate_freshness(
    job: dict,
    discovered_at: datetime,
) -> dict:
    """
    Calculate job age, discovery latency, and freshness window eligibility.
    Formula:
      discovery_latency = scraper_discovered_at - linkedin_posted_at
    """
    posted_at = parse_posted_time(job, reference_time=discovered_at)

    if posted_at is None:
        return {
            "posted_at": None,
            "posted_at_iso": None,
            "discovered_at_iso": discovered_at.isoformat(),
            "discovery_latency_minutes": None,
            "latency_formatted": "Unknown (unparseable timestamp)",
            "is_fresh": False,
            "freshness_reason": (
                "Unparseable or missing posting timestamp; "
                "cannot verify hourly freshness."
            ),
        }

    latency_seconds = (discovered_at - posted_at).total_seconds()
    latency_minutes = round(latency_seconds / 60.0, 1)

    # Format human-readable age
    if latency_minutes < 1:
        age_str = "< 1 minute"
    elif latency_minutes < 60:
        age_str = f"{int(round(latency_minutes))} minutes"
    else:
        hours = round(latency_minutes / 60.0, 1)
        age_str = f"{hours} hours"

    # Enforce freshness window
    # Allow small negative latency (up to -2 mins) for minor server clock desync
    if -2 <= latency_minutes <= FRESHNESS_WINDOW_MINUTES:
        is_fresh = True
        reason = (
            f"Fresh: posted {age_str} ago "
            f"(within {FRESHNESS_WINDOW_MINUTES}m window)"
        )
    elif latency_minutes > FRESHNESS_WINDOW_MINUTES:
        is_fresh = False
        reason = (
            f"Stale: posted {age_str} ago "
            f"(exceeds {FRESHNESS_WINDOW_MINUTES}m window)"
        )
    else:
        is_fresh = False
        reason = f"Invalid future timestamp ({latency_minutes}m ahead)"

    return {
        "posted_at": posted_at,
        "posted_at_iso": posted_at.isoformat(),
        "discovered_at_iso": discovered_at.isoformat(),
        "discovery_latency_minutes": max(0.0, latency_minutes),
        "latency_formatted": age_str,
        "is_fresh": is_fresh,
        "freshness_reason": reason,
    }


# ============================================================
# SCRAPER INTEGRATION (APIFY & BRIGHT DATA)
# ============================================================

def search_jobs_brightdata(run_metadata: dict) -> list[dict]:
    """
    Live on-demand scrape using Bright Data LinkedIn Jobs Scraper API.
    Used when BRIGHT_DATA_API_KEY is configured.
    """
    print()
    print("=" * 70)
    print("BRIGHT DATA LIVE LINKEDIN SCRAPER")
    print("=" * 70)

    url = (
        f"https://api.brightdata.com/datasets/v3/scrape"
        f"?dataset_id={BRIGHT_DATA_DATASET_ID}"
    )

    headers = {
        "Authorization": f"Bearer {BRIGHT_DATA_API_KEY}",
        "Content-Type": "application/json",
    }

    # Pass targeted LinkedIn search URL with 2-hour filter and sort by date
    payload = [
        {
            "url": (
                "https://www.linkedin.com/jobs/search/?"
                "keywords=AI+Engineer&location=India&f_TPR=r7200&sortBy=DD"
            )
        },
        {
            "url": (
                "https://www.linkedin.com/jobs/search/?"
                "keywords=Machine+Learning+Engineer&location=India&f_TPR=r7200&sortBy=DD"
            )
        },
    ]

    print(f"Triggering synchronous scrape via Bright Data ({url})...")

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=180,
        )
        print(f"Bright Data HTTP status: {response.status_code}")

        if response.status_code in (200, 201):
            data = response.json()
            if isinstance(data, list):
                print(f"Bright Data returned {len(data)} jobs.")
                return data
            if isinstance(data, dict) and "items" in data:
                return data["items"]
        else:
            err_msg = (
                f"Bright Data returned HTTP {response.status_code}: "
                f"{response.text[:1000]}"
            )
            print(f"ERROR: {err_msg}")
            run_metadata["scraper_errors"].append(err_msg)

    except requests.RequestException as error:
        err_msg = f"Bright Data request failed: {error}"
        print(f"ERROR: {err_msg}")
        run_metadata["scraper_errors"].append(err_msg)

    return []


def execute_apify_run(
    payload: dict,
    params: dict,
    label: str,
    run_metadata: dict,
) -> tuple[list[dict], dict]:
    """
    Executes a single live request to the Apify LinkedIn scraper actor.
    Captures timing, HTTP status, and response headers (including actor run ID).
    """
    start_dt = datetime.now(timezone.utc)
    t0 = time.time()

    print()
    print(f"[{label}] Starting live Apify invocation at {format_ist_and_utc(start_dt)}...")
    print(f"[{label}] Actor URL: {APIFY_URL}")

    try:
        response = requests.post(
            APIFY_URL,
            params=params,
            json=payload,
            timeout=240,
        )
        duration = round(time.time() - t0, 2)
    except requests.RequestException as error:
        err_msg = f"[{label}] Apify request exception: {error}"
        print(f"ERROR: {err_msg}")
        run_metadata["scraper_errors"].append(err_msg)
        return [], {"error": str(error), "duration": round(time.time() - t0, 2)}

    headers = response.headers
    actor_run_id = (
        headers.get("X-Apify-Actor-Run-Id")
        or headers.get("x-apify-actor-run-id")
        or headers.get("apify-actor-run-id")
        or "Not reported in header"
    )
    dataset_id = (
        headers.get("X-Apify-Dataset-Id")
        or headers.get("x-apify-dataset-id")
        or "Not reported in header"
    )

    print(f"[{label}] HTTP Status: {response.status_code} | Duration: {duration}s")
    print(f"[{label}] Apify Actor Run ID: {actor_run_id} | Dataset ID: {dataset_id}")

    if response.status_code not in (200, 201):
        err_msg = (
            f"[{label}] Apify HTTP {response.status_code}: {response.text[:1000]}"
        )
        print(f"ERROR: {err_msg}")
        run_metadata["scraper_errors"].append(err_msg)
        return [], {
            "status": response.status_code,
            "error": err_msg,
            "duration": duration,
            "actor_run_id": actor_run_id,
        }

    try:
        data = response.json()
    except ValueError:
        err_msg = f"[{label}] Apify returned non-JSON response."
        print(f"ERROR: {err_msg}")
        run_metadata["scraper_errors"].append(err_msg)
        return [], {
            "error": err_msg,
            "duration": duration,
            "actor_run_id": actor_run_id,
        }

    jobs = []
    if isinstance(data, list):
        jobs = data
    elif isinstance(data, dict):
        for key in ("items", "data", "results"):
            if isinstance(data.get(key), list):
                jobs = data[key]
                break

    meta = {
        "label": label,
        "start_utc": start_dt.isoformat(),
        "duration_seconds": duration,
        "status_code": response.status_code,
        "actor_run_id": actor_run_id,
        "dataset_id": dataset_id,
        "total_jobs": len(jobs),
    }
    return jobs, meta


def inspect_and_log_jobs(
    jobs: list[dict],
    reference_time: datetime,
    label: str,
) -> dict:
    """
    Inspects each returned job:
    Extracts LinkedIn numeric ID, raw postedAt text, parsed datetime, and age in minutes.
    Computes summary metrics for hourly freshness analysis.
    """
    print()
    print("=" * 80)
    print(f"[{label}] DETAILED JOB INSPECTION ({len(jobs)} jobs scraped)")
    print(f"Inspection Reference Time: {format_ist_and_utc(reference_time)}")
    print("=" * 80)
    print(
        f"{'#':<3} | {'LinkedIn ID':<12} | {'Age (min)':<10} | "
        f"{'Posted At (UTC)':<18} | {'Title @ Company'}"
    )
    print("-" * 80)

    job_records = []
    youngest_latency = float("inf")
    oldest_latency = 0.0
    jobs_within_60m = 0
    jobs_within_90m = 0
    jobs_over_90m = 0
    unparseable_count = 0

    for idx, job in enumerate(jobs, start=1):
        jid = extract_linkedin_job_id(job) or get_job_id(job)
        title = str(job.get("title") or "Unknown")
        company = str(job.get("companyName") or job.get("company") or "Unknown")
        raw_posted = str(
            job.get("postedAt")
            or job.get("postedDate")
            or job.get("date")
            or ""
        )
        posted_dt = parse_posted_time(job, reference_time=reference_time)

        if posted_dt:
            latency_min = round(
                (reference_time - posted_dt).total_seconds() / 60.0, 1
            )
            youngest_latency = min(youngest_latency, latency_min)
            oldest_latency = max(oldest_latency, latency_min)
            posted_str = posted_dt.strftime("%Y-%m-%d %H:%M")

            if latency_min <= 60:
                jobs_within_60m += 1
            if latency_min <= 90:
                jobs_within_90m += 1
            else:
                jobs_over_90m += 1
            age_display = f"{latency_min}m"
        else:
            latency_min = None
            unparseable_count += 1
            posted_str = f"Raw: {raw_posted[:12]}"
            age_display = "Unknown"

        record = {
            "index": idx,
            "job_id": jid,
            "title": title,
            "company": company,
            "raw_posted": raw_posted,
            "posted_at_utc": posted_str,
            "latency_minutes": latency_min,
            "raw_job": job,
        }
        job_records.append(record)

        # Print all jobs up to 25, plus any fresh job
        if idx <= 25 or (latency_min is not None and latency_min <= 90):
            print(
                f"{idx:<3} | {str(jid):<12} | {age_display:<10} | "
                f"{posted_str:<18} | {title[:25]} @ {company[:20]}"
            )

    if len(jobs) > 25:
        print(f"... [{len(jobs) - 25} more listings inspected] ...")

    summary = {
        "label": label,
        "total": len(jobs),
        "jobs_within_60m": jobs_within_60m,
        "jobs_within_90m": jobs_within_90m,
        "jobs_over_90m": jobs_over_90m,
        "unparseable": unparseable_count,
        "youngest_latency_min": (
            youngest_latency if youngest_latency != float("inf") else None
        ),
        "oldest_latency_min": oldest_latency,
        "job_ids": [r["job_id"] for r in job_records if r["job_id"]],
        "records": job_records,
    }

    print()
    print(f"[{label}] DISTRIBUTION SUMMARY:")
    print(f"  Total Jobs Scraped:                 {summary['total']}")
    print(f"  Jobs posted within 60m (< 1 hour):  {summary['jobs_within_60m']}")
    print(f"  Jobs posted within 90m (< 1.5h):   {summary['jobs_within_90m']}")
    print(f"  Jobs older than 90m (stale):        {summary['jobs_over_90m']}")
    print(f"  Unparseable posting timestamps:     {summary['unparseable']}")
    print(f"  Youngest job posted:                {summary['youngest_latency_min']} minutes ago")
    print(f"  Oldest job posted in batch:         {round(summary['oldest_latency_min'] / 60.0, 1)} hours ago")
    print("-" * 80)

    return summary


def search_jobs_apify(run_metadata: dict) -> list[dict]:
    """
    Live on-demand scrape using Apify actor 'automation-lab~linkedin-jobs-scraper'.

    Production search configuration:
    - sortBy: 'DD' (Most Recent)
    - datePosted: 'r86400' (Past 24 hours max window)
    - startUrls with f_TPR=r7200 (Past 2 hours) and sortBy=DD
    - searchQueries for targeted AI/ML roles in India

    Empirical testing protocol:
    1. Executes Run 1 against live Apify actor and inspects all returned jobs.
    2. Waits 120 seconds.
    3. Executes Run 2 against live Apify actor and inspects all returned jobs.
    4. Compares Actor Run IDs, returned Job IDs, and posting timestamps to determine:
       - Whether the actor actually performs a fresh scrape on every API call.
       - Whether returned results can include jobs posted within the preceding hour.
    """
    print()
    print("=" * 80)
    print("APIFY LIVE LINKEDIN SCRAPER - EMPIRICAL ACQUISITION VERIFICATION")
    print("=" * 80)

    if not APIFY_TOKEN:
        err = "APIFY_API_TOKEN is missing or not set in environment."
        print(f"ERROR: {err}")
        run_metadata["scraper_errors"].append(err)
        return []

    params = {"token": APIFY_TOKEN}

    # Exact production search configuration
    payload = {
        "searchQuery": "AI Engineer",
        "searchQueries": [
            "AI Engineer",
            "Machine Learning Engineer",
            "Generative AI Engineer",
            "LLM Engineer",
            "Agentic AI Engineer",
        ],
        "location": "India",
        "maxJobs": MAX_SCRAPED_JOBS,
        "jobType": "F",
        "experienceLevel": "2",  # Entry level
        "datePosted": "r86400",  # Past 24 hours max window
        "sortBy": "DD",  # Most Recent
        "scrapeJobDetails": True,
        "startUrls": [
            {
                "url": (
                    "https://www.linkedin.com/jobs/search/?"
                    "keywords=AI+Engineer&location=India&f_TPR=r7200&sortBy=DD"
                )
            },
            {
                "url": (
                    "https://www.linkedin.com/jobs/search/?"
                    "keywords=Machine+Learning+Engineer&location=India&f_TPR=r7200&sortBy=DD"
                )
            },
        ],
    }

    print("Search Configuration:")
    print("  sortBy:        'DD' (Most Recent)")
    print("  datePosted:    'r86400' (Past 24 hours)")
    print("  startUrls:     2 URLs with f_TPR='r7200' (Past 2 hours) and sortBy='DD'")
    print(f"  searchQueries: {payload['searchQueries']}")

    # --------------------------------------------------------
    # LIVE RUN 1
    # --------------------------------------------------------
    jobs_1, meta_1 = execute_apify_run(payload, params, "Run 1", run_metadata)
    summary_1 = inspect_and_log_jobs(
        jobs_1,
        datetime.now(timezone.utc),
        "Run 1",
    )

    # --------------------------------------------------------
    # PAUSE & LIVE RUN 2 (COMPARISON TEST)
    # --------------------------------------------------------
    enable_compare = os.environ.get("APIFY_COMPARE_RUNS", "false").lower() == "true"
    if enable_compare:
        sleep_seconds = int(os.environ.get("APIFY_COMPARE_DELAY_SECONDS", "120"))
        print()
        print("=" * 80)
        print(f"PAUSING {sleep_seconds} SECONDS BEFORE RUN 2 (LIVE REPEATABILITY TEST)")
        print("=" * 80)
        print(f"Sleeping {sleep_seconds}s to observe live change in LinkedIn results...")
        time.sleep(sleep_seconds)

        jobs_2, meta_2 = execute_apify_run(payload, params, "Run 2", run_metadata)
        summary_2 = inspect_and_log_jobs(
            jobs_2,
            datetime.now(timezone.utc),
            "Run 2",
        )

        # --------------------------------------------------------
        # COMPARISON & EMPIRICAL EVIDENCE EVALUATION
        # --------------------------------------------------------
        ids_1 = set(summary_1["job_ids"])
        ids_2 = set(summary_2["job_ids"])
        common_ids = ids_1 & ids_2
        new_in_run2 = ids_2 - ids_1
        dropped_in_run2 = ids_1 - ids_2

        overlap_pct = round((len(common_ids) / max(len(ids_1), 1)) * 100, 1)

        print()
        print("=" * 80)
        print("APIFY ACQUISITION LAYER - EMPIRICAL COMPARISON REPORT")
        print("=" * 80)
        print("1. Run Identifiers & Fresh Invocation Proof:")
        print(f"   Run 1 Actor ID: {meta_1.get('actor_run_id')} (Duration: {meta_1.get('duration_seconds')}s)")
        print(f"   Run 2 Actor ID: {meta_2.get('actor_run_id')} (Duration: {meta_2.get('duration_seconds')}s)")
        is_fresh_actor = (
            meta_1.get("actor_run_id") != "Not reported in header"
            and meta_1.get("actor_run_id") != meta_2.get("actor_run_id")
        )
        print(f"   Fresh Actor Spawning Detected: {is_fresh_actor}")

        print()
        print(f"2. Job Set Overlap Across Repeated Runs ({sleep_seconds}s apart):")
        print(f"   Jobs in Run 1:                  {len(ids_1)}")
        print(f"   Jobs in Run 2:                  {len(ids_2)}")
        print(f"   Identical Jobs in Both Runs:    {len(common_ids)} ({overlap_pct}%)")
        print(f"   Newly Appeared in Run 2:        {len(new_in_run2)}")
        print(f"   Dropped in Run 2:               {len(dropped_in_run2)}")

        print()
        print("3. Posting Age & Freshness Distribution:")
        print(f"   Run 1 Youngest Job Age:         {summary_1['youngest_latency_min']} minutes")
        print(f"   Run 2 Youngest Job Age:         {summary_2['youngest_latency_min']} minutes")
        print(f"   Run 1 Jobs <= 60m:              {summary_1['jobs_within_60m']}")
        print(f"   Run 2 Jobs <= 60m:              {summary_2['jobs_within_60m']}")
        print(f"   Run 1 Jobs <= 90m:              {summary_1['jobs_within_90m']}")
        print(f"   Run 2 Jobs <= 90m:              {summary_2['jobs_within_90m']}")

        print()
        print("=" * 80)
        print("EVALUATION OF ACCEPTANCE CRITERIA (9:47 -> 10:00 POSTING DISCOVERY):")
        print("=" * 80)
        has_sub_60m_jobs = (summary_1["jobs_within_60m"] > 0) or (summary_2["jobs_within_60m"] > 0)
        youngest_overall = min(
            x for x in [summary_1["youngest_latency_min"], summary_2["youngest_latency_min"]]
            if x is not None
        ) if (summary_1["youngest_latency_min"] is not None or summary_2["youngest_latency_min"] is not None) else None

        if has_sub_60m_jobs:
            print(f"POSITIVE EVIDENCE: Scraper successfully acquired jobs posted < 60m ago.")
            print(f"Youngest job discovered was posted {youngest_overall} minutes ago.")
        else:
            print("CRITICAL FINDING: Neither run returned any jobs posted within the preceding 60 minutes.")
            print(f"Youngest job observed across both runs: {youngest_overall} minutes ago.")

        # Combine unique jobs from both runs for downstream processing
        seen_unique_ids = set()
        combined_jobs = []
        for j in (jobs_1 + jobs_2):
            jid = extract_linkedin_job_id(j) or get_job_id(j)
            if jid not in seen_unique_ids:
                seen_unique_ids.add(jid)
                combined_jobs.append(j)

        print(f"Total Unique Jobs Prepared for Pipeline: {len(combined_jobs)}")
        print("=" * 80)
        return combined_jobs

    return jobs_1


def search_jobs(run_metadata: dict) -> list[dict]:
    """
    Dispatches to Apify (or Bright Data if explicitly chosen).
    Tags all retrieved items with discovery metadata.
    """
    discovered_at = datetime.now(timezone.utc)
    provider_pref = os.environ.get("SCRAPER_PROVIDER", "apify").lower()

    if provider_pref == "bright_data" and BRIGHT_DATA_API_KEY:
        run_metadata["scraper_provider"] = "bright_data"
        raw_jobs = search_jobs_brightdata(run_metadata)
    else:
        run_metadata["scraper_provider"] = "apify"
        raw_jobs = search_jobs_apify(run_metadata)

    tagged_jobs = []
    for item in raw_jobs:
        if isinstance(item, dict):
            item["_scraper_discovered_at"] = discovered_at
            item["_source_provider"] = run_metadata["scraper_provider"]
            tagged_jobs.append(item)

    run_metadata["jobs_retrieved"] = len(tagged_jobs)
    return tagged_jobs


# ============================================================
# FRESHNESS & DEDUPLICATION STATE MACHINE
# ============================================================

def process_jobs_freshness_and_state(
    raw_jobs: list[dict],
    state: dict,
    discovered_at: datetime,
    run_metadata: dict,
) -> list[dict]:
    """
    Processes raw jobs through freshness calculation and state management.

    Distinguishes:
    1. Previously processed job (status == 'emailed'): SKIP
    2. Failed email attempt (status == 'email_failed'): RETRY ELIGIBLE!
    3. Previously seen but not successfully emailed (status == 'discovered'): RETRY ELIGIBLE!
    4. Newly discovered job: Calculate freshness and process
    5. Stale job: Record as stale, DO NOT email
    """
    seen_jobs = state.setdefault("jobs", {})
    eligible_jobs = []
    current_run_seen_ids = set()

    print()
    print("=" * 70)
    print("FRESHNESS & DEDUPLICATION PIPELINE")
    print("=" * 70)
    print(
        f"Scraper Run Time: {format_ist_and_utc(discovered_at)}"
    )
    print(
        f"Freshness Window: last {FRESHNESS_WINDOW_MINUTES} minutes"
    )

    for job in raw_jobs:
        job_id = get_job_id(job)
        job["_job_id"] = job_id
        canonical_url = get_canonical_job_url(job, job_id)
        job["_canonical_url"] = canonical_url

        # Prevent duplicate items within the same scrape response
        if job_id in current_run_seen_ids:
            continue
        current_run_seen_ids.add(job_id)

        # 1. Freshness Calculation
        freshness = calculate_freshness(job, discovered_at)
        job["_freshness"] = freshness
        job["_posted_at_dt"] = freshness["posted_at"]
        job["_discovery_latency_minutes"] = freshness["discovery_latency_minutes"]
        job["_latency_formatted"] = freshness["latency_formatted"]

        existing_record = seen_jobs.get(job_id)

        # 2. Check if already successfully emailed
        if (
            existing_record
            and existing_record.get("status") == "emailed"
        ):
            run_metadata["duplicates_skipped"] += 1
            existing_record["last_seen"] = discovered_at.isoformat()
            continue

        # 3. Check for stale jobs
        if not freshness["is_fresh"]:
            run_metadata["stale_jobs_filtered"] += 1
            print(
                f"[STALE FILTERED] '{job.get('title')}' at "
                f"'{job.get('companyName')}' - "
                f"{freshness['freshness_reason']}"
            )

            # Record in state as stale so we don't re-log or re-evaluate it
            if job_id not in seen_jobs:
                seen_jobs[job_id] = {
                    "job_id": job_id,
                    "title": str(job.get("title") or ""),
                    "company": str(
                        job.get("companyName")
                        or job.get("company")
                        or ""
                    ),
                    "first_seen": discovered_at.isoformat(),
                    "last_seen": discovered_at.isoformat(),
                    "posted_at": freshness["posted_at_iso"],
                    "discovery_latency_minutes": freshness[
                        "discovery_latency_minutes"
                    ],
                    "status": "stale",
                    "reason": freshness["freshness_reason"],
                }
            continue

        # Job is within freshness window!
        run_metadata["jobs_within_freshness_window"] += 1

        # 4. Handle Retry vs New Discovery
        if existing_record and existing_record.get("status") in (
            "email_failed",
            "discovered",
        ):
            print(
                f"[RETRY ELIGIBLE] '{job.get('title')}' at "
                f"'{job.get('companyName')}' (Prior status: "
                f"{existing_record.get('status')})"
            )
            run_metadata["email_retries"] += 1
            existing_record["last_seen"] = discovered_at.isoformat()
        else:
            print(
                f"[FRESH DISCOVERY] '{job.get('title')}' at "
                f"'{job.get('companyName')}' | "
                f"Latency: {freshness['latency_formatted']} | "
                f"Posted: {format_ist_and_utc(freshness['posted_at'])}"
            )
            run_metadata["new_fresh_jobs"] += 1
            # Mark as discovered (pending match and email)
            seen_jobs[job_id] = {
                "job_id": job_id,
                "title": str(job.get("title") or ""),
                "company": str(
                    job.get("companyName")
                    or job.get("company")
                    or ""
                ),
                "url": canonical_url,
                "first_seen": discovered_at.isoformat(),
                "last_seen": discovered_at.isoformat(),
                "posted_at": freshness["posted_at_iso"],
                "discovery_latency_minutes": freshness[
                    "discovery_latency_minutes"
                ],
                "status": "discovered",
                "email_attempts": 0,
            }

        eligible_jobs.append(job)

    print()
    print(
        f"Freshness & State Summary: "
        f"{len(raw_jobs)} scraped -> "
        f"{run_metadata['jobs_within_freshness_window']} fresh (< {FRESHNESS_WINDOW_MINUTES}m) -> "
        f"{run_metadata['stale_jobs_filtered']} stale filtered -> "
        f"{run_metadata['duplicates_skipped']} duplicates skipped -> "
        f"{len(eligible_jobs)} eligible for matching"
    )

    run_metadata["eligible_jobs"] = len(eligible_jobs)
    return eligible_jobs


# ============================================================
# MATCHING & RANKING
# ============================================================

def score_and_rank_jobs(
    jobs: list[dict],
    run_metadata: dict,
) -> list[dict]:
    """
    Two-stage matching pipeline:
    1. Deterministic local relevance filter (AI/ML engineering profile)
    2. Single Groq batch request (with local fallback)
    """
    if not jobs:
        print("No jobs available for matching.")
        return []

    print()
    print("=" * 70)
    print("LOCAL JOB RELEVANCE FILTER")
    print("=" * 70)

    candidates = locally_filter_jobs(jobs)
    run_metadata["candidates_surviving_filter"] = len(candidates)

    if not candidates:
        print("No jobs passed the local AI/ML filter.")
        return []

    print()
    print("=" * 70)
    print("AI BATCH MATCHING (GROQ / LOCAL FALLBACK)")
    print("=" * 70)

    scored_jobs = score_jobs_batch(candidates)
    if not scored_jobs:
        return []

    scored_jobs.sort(
        key=lambda j: j.get("match_score", 0),
        reverse=True,
    )

    matched_jobs = [
        j
        for j in scored_jobs
        if j.get("match_score", 0) >= MIN_MATCH_SCORE
    ]

    run_metadata["high_match_jobs"] = len(matched_jobs)

    print()
    print(
        f"Jobs meeting {MIN_MATCH_SCORE}+ match threshold: "
        f"{len(matched_jobs)}"
    )

    return matched_jobs


# ============================================================
# EMAIL REPORTING WITH FRESHNESS & RUN METADATA
# ============================================================

def send_email_report(
    jobs: list[dict],
    run_metadata: dict,
    state: dict,
):
    """
    Send hourly job report via Gmail SMTP.
    Features:
    - Prominently displays Posted Time, Discovered Time, and Latency for every job
    - Includes execution timeline & run metadata header
    - Atomic status transition: sets status='emailed' only upon verified send;
      sets status='email_failed' on error so jobs remain eligible for retry!
    """
    username = GMAIL_USERNAME
    app_password = GMAIL_APP_PASSWORD

    if not username or not app_password:
        print(
            "WARNING: Gmail credentials not configured. "
            "Skipping email delivery."
        )
        return

    now_utc = datetime.now(timezone.utc)
    run_metadata["run_end_utc"] = now_utc.isoformat()
    run_metadata["run_end_ist"] = format_ist_and_utc(now_utc)

    message = EmailMessage()
    message["From"] = username
    message["To"] = username

    count = len(jobs[:MAX_EMAIL_JOBS])
    message["Subject"] = (
        f"AI Job Hunter - {count} High-Match Fresh Jobs "
        f"[{format_ist_and_utc(now_utc)}]"
    )

    lines = [
        "AI JOB HUNTER - HOURLY FRESHNESS REPORT",
        "=" * 70,
        "",
        "RUN METADATA:",
        f"  Run Time: {format_ist_and_utc(now_utc)}",
        f"  Scraper Provider: {run_metadata.get('scraper_provider', 'apify')}",
        f"  Freshness Window: Last {FRESHNESS_WINDOW_MINUTES} minutes",
        f"  Jobs Retrieved: {run_metadata.get('jobs_retrieved', 0)}",
        (
            f"  Within Freshness Window: "
            f"{run_metadata.get('jobs_within_freshness_window', 0)}"
        ),
        f"  Stale Jobs Filtered: {run_metadata.get('stale_jobs_filtered', 0)}",
        f"  Duplicates Skipped: {run_metadata.get('duplicates_skipped', 0)}",
        f"  Email Retries: {run_metadata.get('email_retries', 0)}",
        (
            f"  AI Relevance Candidates: "
            f"{run_metadata.get('candidates_surviving_filter', 0)}"
        ),
        (
            f"  High-Match (>= {MIN_MATCH_SCORE}): "
            f"{run_metadata.get('high_match_jobs', 0)}"
        ),
    ]

    if run_metadata.get("scraper_errors"):
        lines.append(
            f"  Scraper Warnings: {run_metadata['scraper_errors']}"
        )

    lines.extend(["", "=" * 70, ""])

    if not jobs:
        lines.extend(
            [
                "No fresh jobs met the AI match threshold during this run.",
                "",
                "DIAGNOSIS:",
                f"- Scraper ran: YES ({run_metadata.get('jobs_retrieved', 0)} jobs seen)",
                (
                    f"- Jobs within last {FRESHNESS_WINDOW_MINUTES} mins: "
                    f"{run_metadata.get('jobs_within_freshness_window', 0)}"
                ),
                (
                    f"- Stale jobs rejected: "
                    f"{run_metadata.get('stale_jobs_filtered', 0)}"
                ),
                (
                    f"- Previously processed jobs skipped: "
                    f"{run_metadata.get('duplicates_skipped', 0)}"
                ),
                (
                    f"- Candidates surviving local filter: "
                    f"{run_metadata.get('candidates_surviving_filter', 0)}"
                ),
                "",
                "Next hourly scan will execute automatically.",
                "",
                "=" * 70,
            ]
        )
    else:
        for index, job in enumerate(jobs[:MAX_EMAIL_JOBS], start=1):
            title = job.get("title", "Unknown Title")
            company = (
                job.get("companyName")
                or job.get("company")
                or "Unknown Company"
            )
            location = job.get("location", "India")
            job_id = job.get("_job_id", "N/A")
            canonical_url = job.get("_canonical_url", "")
            apply_url = (
                job.get("applyUrl")
                or canonical_url
                or "No link available"
            )

            posted_dt = job.get("_posted_at_dt")
            posted_str = format_ist_and_utc(posted_dt)
            discovered_dt = job.get(
                "_scraper_discovered_at",
                now_utc,
            )
            discovered_str = format_ist_and_utc(discovered_dt)
            age_str = job.get(
                "_latency_formatted",
                "Unknown",
            )

            score = job.get("match_score", 0)
            qualification = job.get(
                "qualification",
                "MODERATE_MATCH",
            )
            exp_fit = job.get("experience_fit", "MODERATE")
            tech_fit = job.get("technical_fit", 0)
            role_fit = job.get("role_fit", 0)
            reason = job.get("match_reason", "")
            key_matches = job.get("key_matches", [])
            concerns = job.get("concerns", [])

            lines.extend(
                [
                    f"{index}. {title}",
                    f"   Company: {company}",
                    f"   Location: {location}",
                    "",
                    f"   Posted: {posted_str}",
                    f"   Discovered: {discovered_str}",
                    f"   Age when discovered: {age_str}",
                    f"   LinkedIn Job ID: {job_id}",
                    "",
                    f"   AI MATCH SCORE: {score}/100 ({qualification})",
                    (
                        f"   Fit Breakdown: Tech {tech_fit}/100 | "
                        f"Role {role_fit}/100 | Experience: {exp_fit}"
                    ),
                    "",
                    f"   Apply: {apply_url}",
                    f"   LinkedIn: {canonical_url}",
                    "",
                    f"   WHY IT MATCHES: {reason}",
                ]
            )

            if key_matches:
                lines.append("   KEY MATCHES:")
                for km in key_matches:
                    lines.append(f"     + {km}")

            if concerns:
                lines.append("   NOTES / CONCERNS:")
                for c in concerns:
                    lines.append(f"     ! {c}")

            lines.extend(["", "-" * 70, ""])

    message.set_content("\n".join(lines))

    print()
    print("Connecting to Gmail SMTP (smtp.gmail.com:465)...")

    seen_jobs = state.setdefault("jobs", {})

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(username, app_password)
            server.send_message(message)

        print("Email sent successfully!")
        run_metadata["emails_sent"] = len(jobs[:MAX_EMAIL_JOBS])
        run_metadata["email_status"] = "success"

        # Update state: mark successfully emailed jobs
        sent_now = datetime.now(timezone.utc).isoformat()
        for job in jobs[:MAX_EMAIL_JOBS]:
            jid = job.get("_job_id")
            if jid and jid in seen_jobs:
                seen_jobs[jid]["status"] = "emailed"
                seen_jobs[jid]["emailed_at"] = sent_now
                seen_jobs[jid]["match_score"] = job.get("match_score", 0)

    except Exception as error:
        print(f"ERROR: Gmail delivery failed: {error}")
        run_metadata["email_status"] = "failed"
        run_metadata["email_error"] = str(error)
        run_metadata["email_failures"] = len(jobs[:MAX_EMAIL_JOBS])

        # Mark jobs as email_failed so they remain eligible for retry next run!
        for job in jobs[:MAX_EMAIL_JOBS]:
            jid = job.get("_job_id")
            if jid and jid in seen_jobs:
                seen_jobs[jid]["status"] = "email_failed"
                seen_jobs[jid]["last_error"] = str(error)
                seen_jobs[jid]["email_attempts"] = (
                    seen_jobs[jid].get("email_attempts", 0) + 1
                )
        raise


# ============================================================
# MAIN PIPELINE
# ============================================================

def main():
    """
    Main Hourly AI Job Hunter Execution Pipeline:

    Scheduler (Hourly)
      -> Scraper (Live Apify / Bright Data)
      -> Normalization & Stable ID Extraction
      -> Freshness Pipeline (discovery_latency <= 90 mins)
      -> Deduplication & State Machine
      -> Relevance & AI Batch Matching
      -> Email Notification with Freshness Metrics
      -> Failsafe State Persistence
    """
    start_time = datetime.now(timezone.utc)

    run_metadata = {
        "run_start_utc": start_time.isoformat(),
        "run_start_ist": format_ist_and_utc(start_time),
        "run_end_utc": None,
        "run_end_ist": None,
        "scraper_provider": "apify",
        "jobs_retrieved": 0,
        "jobs_within_freshness_window": 0,
        "stale_jobs_filtered": 0,
        "duplicates_skipped": 0,
        "email_retries": 0,
        "new_fresh_jobs": 0,
        "eligible_jobs": 0,
        "candidates_surviving_filter": 0,
        "high_match_jobs": 0,
        "emails_sent": 0,
        "email_failures": 0,
        "email_status": "not_attempted",
        "scraper_errors": [],
        "workflow_errors": [],
    }

    print("=" * 70)
    print("HOURLY AI JOB HUNTER STARTED")
    print(f"Start Time: {format_ist_and_utc(start_time)}")
    print("=" * 70)

    # 1. Load State
    state = load_state()
    print(f"Tracked jobs in database: {len(state.get('jobs', {}))}")

    try:
        # 2. Scrape Jobs (Live & Fresh)
        raw_jobs = search_jobs(run_metadata)

        # 3. Freshness & Deduplication State Machine
        eligible_jobs = process_jobs_freshness_and_state(
            raw_jobs,
            state,
            start_time,
            run_metadata,
        )

        # 4. Local Relevance + AI Batch Matching
        matched_jobs = score_and_rank_jobs(
            eligible_jobs,
            run_metadata,
        )

        # 5. Send Email Report
        print()
        print("=" * 70)
        print("EMAIL REPORT")
        print("=" * 70)
        send_email_report(
            jobs=matched_jobs,
            run_metadata=run_metadata,
            state=state,
        )

    except Exception as exc:
        print(f"WORKFLOW EXCEPTION: {exc}")
        run_metadata["workflow_errors"].append(str(exc))

    finally:
        end_time = datetime.now(timezone.utc)
        run_metadata["run_end_utc"] = end_time.isoformat()
        run_metadata["run_end_ist"] = format_ist_and_utc(end_time)
        state["last_run"] = run_metadata

        # Always save state to disk
        save_state(state)

        print()
        print("=" * 70)
        print("RUN EXECUTION SUMMARY")
        print("=" * 70)
        print(f"Start: {run_metadata['run_start_ist']}")
        print(f"End:   {run_metadata['run_end_ist']}")
        print(f"Provider: {run_metadata['scraper_provider']}")
        print(f"Scraped: {run_metadata['jobs_retrieved']}")
        print(f"Fresh (< {FRESHNESS_WINDOW_MINUTES}m): {run_metadata['jobs_within_freshness_window']}")
        print(f"Stale filtered: {run_metadata['stale_jobs_filtered']}")
        print(f"Duplicates skipped: {run_metadata['duplicates_skipped']}")
        print(f"High-match jobs: {run_metadata['high_match_jobs']}")
        print(f"Email Status: {run_metadata['email_status']}")
        print("=" * 70)


if __name__ == "__main__":
    main()
