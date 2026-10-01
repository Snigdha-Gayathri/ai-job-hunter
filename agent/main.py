import json
import os
import re
import smtplib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path

import sys
import requests

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from job_matcher import (
    locally_filter_jobs,
    score_jobs_batch,
    classify_priority,
)
from sources import (
    SourceRegistry,
    normalize_url,
)
from config import (
    SOURCES_CONFIG,
    HIGH_PRIORITY_SCORE,
    FRESHNESS_WINDOW_MINUTES,
    FRESHNESS_WINDOW_HOURS,
    HIGH_PRIORITY_MAX_AGE_MINUTES,
    TARGET_LOCATIONS as CONFIG_TARGET_LOCATIONS,
    TARGET_ROLE_FAMILIES,
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
# Configured via FRESHNESS_WINDOW_MINUTES (default: 48 hours for active postings)
# High-priority alert window: posted within last 3 hours (180 mins)

# Limits
MAX_EMAIL_JOBS = 20
MAX_SCRAPED_JOBS = 50
MIN_MATCH_SCORE = 75

# Persistent state management
BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("STATE_DIR", str(BASE_DIR / "state")))
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
            "sources": {},
            "last_run": {},
        }

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            state = json.load(file)

        if not isinstance(state, dict):
            return {"jobs": {}, "sources": {}, "last_run": {}}

        if "jobs" not in state or not isinstance(state["jobs"], dict):
            state["jobs"] = {}

        if "sources" not in state or not isinstance(state["sources"], dict):
            state["sources"] = {}

        if "last_run" not in state or not isinstance(state["last_run"], dict):
            state["last_run"] = {}

        return state

    except Exception as error:
        print(f"WARNING: Could not load state file: {error}")
        return {"jobs": {}, "sources": {}, "last_run": {}}


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
        temp_file = STATE_FILE.with_suffix(".tmp")
        with temp_file.open("w", encoding="utf-8") as file:
            json.dump(state, file, indent=2)
        temp_file.replace(STATE_FILE)

        if "last_run" in state and state["last_run"]:
            metrics_temp = METRICS_FILE.with_suffix(".tmp")
            with metrics_temp.open("w", encoding="utf-8") as file:
                json.dump(state["last_run"], file, indent=2)
            metrics_temp.replace(METRICS_FILE)

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


def normalize_string_key(s: str) -> str:
    """Strip punctuation and whitespace for fuzzy cross-source matching."""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def get_job_id(job: dict) -> str:
    """
    Generate a stable, unique identifier for a job.
    Preference:
    1. LinkedIn numeric Job ID (exact backward compatibility)
    2. Explicit source_job_id (ATS/job boards)
    3. Canonical URL
    4. Normalized title|company|location
    """
    lid = extract_linkedin_job_id(job)
    if lid:
        return lid

    source_job_id = job.get("source_job_id")
    if source_job_id:
        return str(source_job_id).strip()

    raw_url = str(job.get("jobUrl") or job.get("url") or "").strip()
    if raw_url:
        clean_url = normalize_url(raw_url)
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
    Return clean canonical URL without tracking tokens.
    """
    if job_id and job_id.isdigit():
        return f"https://www.linkedin.com/jobs/view/{job_id}"

    raw_url = (
        job.get("jobUrl")
        or job.get("url")
        or job.get("applyUrl")
        or job.get("apply_url")
        or ""
    )
    if raw_url:
        return normalize_url(raw_url)
    return "No URL available"


def find_existing_job(job: dict, seen_jobs: dict) -> tuple[str | None, dict | None]:
    """
    Cross-source deduplication search with conservative identity evidence.
    Avoids false merges across genuinely different job openings at the same company:
    1. Direct job_id match
    2. Canonical URL match (normalized without tracking params)
    3. Direct application URL match (e.g. LinkedIn/aggregator applyUrl points to ATS board URL)
    4. Explicit ATS / source job identifier match
    """
    job_id = job.get("_job_id") or get_job_id(job)
    if job_id in seen_jobs:
        return job_id, seen_jobs[job_id]

    target_url = job.get("_canonical_url") or get_canonical_job_url(job, job_id)
    target_apply_url = normalize_url(job.get("apply_url") or job.get("applyUrl") or "")
    target_source_id = str(job.get("source_job_id") or "").strip()

    for existing_id, record in seen_jobs.items():
        rec_url = record.get("url", "")
        rec_apply_url = normalize_url(record.get("apply_url") or record.get("applyUrl") or "")
        rec_source_id = str(record.get("source_job_id") or "").strip()

        # 1. Canonical URL match
        if target_url and rec_url and target_url == rec_url:
            return existing_id, record

        # 2. Application endpoint cross-reference match
        if target_url and rec_apply_url and target_url == rec_apply_url:
            return existing_id, record
        if target_apply_url and rec_url and target_apply_url == rec_url:
            return existing_id, record
        if target_apply_url and rec_apply_url and target_apply_url == rec_apply_url:
            return existing_id, record

        # 3. Explicit stable source identifier match (e.g. gh_999 or rok_123)
        if target_source_id and rec_source_id and target_source_id == rec_source_id:
            return existing_id, record

    return None, None


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

    # Check if already a datetime
    for k in ("source_posted_at", "posted_at", "postedAt", "published_at"):
        val = job.get(k)
        if isinstance(val, datetime):
            if val.tzinfo is None:
                val = val.replace(tzinfo=timezone.utc)
            return val.astimezone(timezone.utc)

    raw = (
        job.get("source_posted_at")
        or job.get("posted_at")
        or job.get("postedAt")
        or job.get("postedDate")
        or job.get("postedAtText")
        or job.get("date")
        or job.get("date_text")
        or job.get("time")
        or job.get("updated_at")
        or job.get("publishedAt")
        or job.get("pubDate")
        or job.get("pub_date")
        or job.get("publication_date")
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
    posted_at = job.get("source_posted_at")
    if not isinstance(posted_at, datetime):
        posted_at = parse_posted_time(job, reference_time=discovered_at)

    source_name = str(job.get("source") or "").lower()

    if posted_at is None:
        raw_posted = str(
            job.get("postedAt")
            or job.get("postedDate")
            or job.get("postedAtText")
            or job.get("date")
            or ""
        ).strip()
        # If source has no date field at all and is not LinkedIn, treat as fresh on initial discovery
        if not raw_posted and source_name and source_name != "linkedin":
            return {
                "posted_at": None,
                "posted_at_iso": None,
                "discovered_at_iso": discovered_at.isoformat(),
                "discovery_latency_minutes": None,
                "latency_formatted": "Newly discovered (no source timestamp)",
                "is_fresh": True,
                "freshness_reason": "Freshly acquired from source without explicit posting timestamp.",
            }
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
    elif latency_minutes < 1440:
        hours = round(latency_minutes / 60.0, 1)
        age_str = f"{hours} hours"
    else:
        days = round(latency_minutes / 1440.0, 1)
        age_str = f"{days} days"

    # Source-specific freshness window:
    # LinkedIn: fast-stream window (default 90m for hourly freshness, configurable)
    # ATS and Remote boards: active posting window (48 hours = 2880m)
    if source_name in ("greenhouse", "lever", "ashby", "remoteok", "remotive", "workingnomads", "weworkremotely", "nodesk"):
        source_window_minutes = int(os.environ.get("ATS_FRESHNESS_WINDOW_MINUTES", "2880"))
    else:
        source_window_minutes = FRESHNESS_WINDOW_MINUTES

    # Enforce freshness window
    # Allow small negative latency (up to -5 mins) for minor server clock desync
    if -5 <= latency_minutes <= source_window_minutes:
        is_fresh = True
        reason = (
            f"Fresh: posted {age_str} ago "
            f"(within {source_window_minutes}m window)"
        )
    elif latency_minutes > source_window_minutes:
        is_fresh = False
        reason = (
            f"Stale: posted {age_str} ago "
            f"(exceeds {source_window_minutes}m window)"
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


source_registry = SourceRegistry()


def search_jobs(
    run_metadata: dict,
    state: dict | None = None,
    force_all: bool = False,
) -> list[dict]:
    """
    Multi-source acquisition dispatcher with ThreadPoolExecutor concurrency:
    1. Determines due sources based on source-specific polling intervals.
    2. Fetches all due sources concurrently so slow sources never block fast sources.
    3. Failure in any single source is fully isolated and does not crash the pipeline.
    4. Independent timeout, failure handling, and latency logging per source.
    """
    discovered_at = datetime.now(timezone.utc)
    source_states = state.setdefault("sources", {}) if state is not None else {}
    tagged_jobs = []

    t_src_start = time.time()
    source_stats = run_metadata.setdefault("source_stats", {})

    # Check if LinkedIn is due
    linkedin_due = force_all or (
        source_states.get("linkedin", {}).get("last_polled") is None
        or (discovered_at - datetime.fromisoformat(source_states["linkedin"]["last_polled"])).total_seconds() >= 25 * 60
    )

    # Get due sources from registry
    due_sources = source_registry.get_due_sources(discovered_at, source_states, force_all=force_all)

    def fetch_single_source(src):
        t_s = time.time()
        try:
            raw_items = src.fetch()
            normalized = []
            for item in raw_items:
                norm = src.normalize(item, discovered_at)
                normalized.append(norm)
            elapsed = round(time.time() - t_s, 2)
            if normalized:
                status = "OK"
            elif src.source_id in ("remote100k", "justremote", "indeed", "wellfound", "naukri", "instahyre", "cutshort", "foundit", "hirist"):
                status = "LIMITED"
            elif src.source_id in ("skipthedrive", "remoteco"):
                status = "UNAVAILABLE"
            else:
                status = "EMPTY"
            return src.source_id, src.name, normalized, status, elapsed, None
        except Exception as error:
            elapsed = round(time.time() - t_s, 2)
            err_msg = f"{type(error).__name__}: {str(error)}"
            return src.source_id, src.name, [], "FAILED", elapsed, err_msg

    def fetch_linkedin():
        t_li = time.time()
        provider_pref = os.environ.get("SCRAPER_PROVIDER", "apify").lower()
        try:
            if provider_pref == "bright_data" and BRIGHT_DATA_API_KEY:
                run_metadata["scraper_provider"] = "bright_data"
                raw = search_jobs_brightdata(run_metadata)
            elif APIFY_TOKEN:
                run_metadata["scraper_provider"] = "apify"
                raw = search_jobs_apify(run_metadata)
            else:
                return "linkedin", "LinkedIn (Apify)", [], "UNAVAILABLE", round(time.time() - t_li, 2), "APIFY_API_TOKEN not configured"
            status = "OK" if raw else "EMPTY"
            return "linkedin", "LinkedIn", raw, status, round(time.time() - t_li, 2), None
        except Exception as error:
            err_msg = f"{type(error).__name__}: {str(error)}"
            return "linkedin", "LinkedIn", [], "FAILED", round(time.time() - t_li, 2), err_msg

    total_tasks = len(due_sources) + (1 if linkedin_due else 0)
    print()
    print("=" * 70)
    print(f"PARALLEL MULTI-SOURCE ACQUISITION ({total_tasks} sources due: LinkedIn={linkedin_due}, Registry={len(due_sources)})")
    print("=" * 70)

    if total_tasks > 0:
        max_workers = min(total_tasks, 8)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_sid = {}

            if linkedin_due:
                source_states.setdefault("linkedin", {})["last_polled"] = discovered_at.isoformat()
                fut = executor.submit(fetch_linkedin)
                future_to_sid[fut] = "linkedin"

            for src in due_sources:
                fut = executor.submit(fetch_single_source, src)
                future_to_sid[fut] = src.source_id

            for fut in as_completed(future_to_sid):
                sid = future_to_sid[fut]
                try:
                    source_id, source_name, jobs, status, elapsed, err = fut.result()
                except Exception as exc:
                    source_id = sid
                    source_name = sid.title()
                    jobs = []
                    status = "FAILED"
                    elapsed = 0.0
                    err = str(exc)

                print(f"  [{source_name}] Completed in {elapsed}s | Status: {status} | Jobs: {len(jobs)}")

                s_state = source_states.setdefault(source_id, {
                    "last_polled": discovered_at.isoformat(),
                    "last_success": None,
                    "last_failure": None,
                    "consecutive_failures": 0,
                    "jobs_fetched": 0,
                    "last_error": None,
                })
                s_state["last_polled"] = discovered_at.isoformat()

                if err:
                    run_metadata["scraper_errors"].append(f"[{source_name}] {err}")
                    s_state["last_failure"] = discovered_at.isoformat()
                    s_state["consecutive_failures"] = s_state.get("consecutive_failures", 0) + 1
                    s_state["last_error"] = err
                    source_stats[source_id] = {
                        "name": source_name,
                        "status": "FAILED",
                        "raw": 0,
                        "valid": 0,
                        "error": err,
                        "elapsed": elapsed,
                    }
                else:
                    s_state["last_success"] = discovered_at.isoformat()
                    s_state["consecutive_failures"] = 0
                    s_state["jobs_fetched"] = s_state.get("jobs_fetched", 0) + len(jobs)
                    s_state["last_error"] = None
                    source_stats[source_id] = {
                        "name": source_name,
                        "status": status,
                        "raw": len(jobs),
                        "valid": len(jobs),
                        "elapsed": elapsed,
                    }
                    for item in jobs:
                        if isinstance(item, dict):
                            item["_scraper_discovered_at"] = discovered_at
                            item["_source_provider"] = source_id
                            item.setdefault("source", source_id)
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
    6. Multi-source deduplication: Collapses same job across multiple sources
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
        source_name = job.get("source") or job.get("_source_provider") or "linkedin"
        job["source"] = source_name

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

        # 2. Cross-Source Deduplication Check
        existing_id, existing_record = find_existing_job(job, seen_jobs)
        if existing_id:
            job_id = existing_id
            job["_job_id"] = existing_id

        # If already finalized (emailed, rejected, stale, email_abandoned): skip!
        if existing_record and existing_record.get("status") in (
            "emailed",
            "rejected",
            "stale",
            "email_abandoned",
        ):
            run_metadata["duplicates_skipped"] += 1
            existing_record["last_seen"] = discovered_at.isoformat()
            seen_sources = existing_record.setdefault("seen_sources", [existing_record.get("source", "linkedin")])
            if source_name not in seen_sources:
                seen_sources.append(source_name)
            continue

        # Check retry limit for failed email attempts (max 3 retries)
        if existing_record and existing_record.get("status") == "email_failed":
            attempts = existing_record.get("email_attempts", 0)
            if attempts >= 3:
                existing_record["status"] = "email_abandoned"
                run_metadata["duplicates_skipped"] += 1
                print(
                    f"[RETRY ABANDONED] Max email attempts (3) exceeded for "
                    f"'{job.get('title')}' at '{job.get('companyName') or job.get('company')}'"
                )
                continue

        # 3. Check for stale jobs
        if not freshness["is_fresh"]:
            run_metadata["stale_jobs_filtered"] += 1
            print(
                f"[STALE FILTERED] '{job.get('title')}' at "
                f"'{job.get('companyName') or job.get('company')}' - "
                f"{freshness['freshness_reason']}"
            )

            # Record in state as stale so we don't re-log or re-evaluate it
            if job_id not in seen_jobs:
                seen_jobs[job_id] = {
                    "job_id": job_id,
                    "source_job_id": job.get("source_job_id") or job_id,
                    "title": str(job.get("title") or ""),
                    "company": str(
                        job.get("companyName")
                        or job.get("company")
                        or ""
                    ),
                    "url": canonical_url,
                    "source": source_name,
                    "seen_sources": [source_name],
                    "first_seen": discovered_at.isoformat(),
                    "first_seen_at": discovered_at.isoformat(),
                    "last_seen": discovered_at.isoformat(),
                    "posted_at": freshness["posted_at_iso"],
                    "source_posted_at": freshness["posted_at_iso"],
                    "discovery_latency_minutes": freshness[
                        "discovery_latency_minutes"
                    ],
                    "detection_latency_minutes": freshness[
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
                f"'{job.get('companyName') or job.get('company')}' (Prior status: "
                f"{existing_record.get('status')})"
            )
            run_metadata["email_retries"] += 1
            existing_record["last_seen"] = discovered_at.isoformat()
            seen_sources = existing_record.setdefault("seen_sources", [existing_record.get("source", "linkedin")])
            if source_name not in seen_sources:
                seen_sources.append(source_name)

            # If match decision is already known, reuse it rather than calling Groq again
            if existing_record.get("match_score") is not None:
                job["match_score"] = existing_record.get("match_score")
                job["qualification"] = existing_record.get("qualification", "GOOD_MATCH")
                job["priority"] = existing_record.get("priority", "HIGH")
                job["_already_scored"] = True
        else:
            print(
                f"[FRESH DISCOVERY] '{job.get('title')}' at "
                f"'{job.get('companyName') or job.get('company')}' [{source_name.upper()}] | "
                f"Latency: {freshness['latency_formatted']} | "
                f"Posted: {format_ist_and_utc(freshness['posted_at'])}"
            )
            run_metadata["new_fresh_jobs"] = run_metadata.get("new_fresh_jobs", 0) + 1
            # Mark as discovered (pending match and email)
            now_utc = datetime.now(timezone.utc)
            seen_jobs[job_id] = {
                "job_id": job_id,
                "source_job_id": job.get("source_job_id") or job_id,
                "title": str(job.get("title") or ""),
                "company": str(
                    job.get("companyName")
                    or job.get("company")
                    or ""
                ),
                "url": canonical_url,
                "source": source_name,
                "seen_sources": [source_name],
                "first_seen": discovered_at.isoformat(),
                "first_seen_at": discovered_at.isoformat(),
                "last_seen": discovered_at.isoformat(),
                "processed_at": now_utc.isoformat(),
                "notified_at": None,
                "posted_at": freshness["posted_at_iso"],
                "source_posted_at": freshness["posted_at_iso"],
                "discovery_latency_minutes": freshness[
                    "discovery_latency_minutes"
                ],
                "detection_latency_minutes": freshness[
                    "discovery_latency_minutes"
                ],
                "notification_latency_minutes": None,
                "priority": "LOW",
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
    state: dict | None = None,
) -> list[dict]:
    """
    Two-stage matching pipeline:
    1. Deterministic local relevance filter (AI/ML engineering profile)
    2. Single Groq batch request (with local fallback)
    Records match outcomes and rejection states in state to prevent redundant Groq calls.
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

    seen_jobs = state.setdefault("jobs", {}) if state is not None else {}

    # Record jobs that failed local pre-filter as rejected so they are never evaluated again
    candidate_ids = {c.get("_job_id") for c in candidates if c.get("_job_id")}
    for job in jobs:
        jid = job.get("_job_id")
        if jid and jid not in candidate_ids and jid in seen_jobs:
            seen_jobs[jid]["status"] = "rejected"
            seen_jobs[jid]["reason"] = "local_prefilter_rejected"

    if not candidates:
        print("No jobs passed the local AI/ML filter.")
        return []

    # Separate candidates: new un-scored vs previously evaluated retries
    unscored_candidates = [j for j in candidates if not j.get("_already_scored")]
    already_scored_candidates = [j for j in candidates if j.get("_already_scored")]

    scored_new = []
    if unscored_candidates:
        print()
        print("=" * 70)
        print("AI BATCH MATCHING (GROQ / LOCAL FALLBACK)")
        print("=" * 70)

        scored_new = score_jobs_batch(unscored_candidates)
        if scored_new:
            for j in scored_new:
                j["priority"] = classify_priority(j)
                jid = j.get("_job_id")
                if jid and jid in seen_jobs:
                    score = j.get("match_score", 0)
                    seen_jobs[jid]["match_score"] = score
                    seen_jobs[jid]["qualification"] = j.get("qualification", "MODERATE_MATCH")
                    seen_jobs[jid]["priority"] = j.get("priority", "LOW")
                    seen_jobs[jid]["reason"] = j.get("reason", "")
                    if score < MIN_MATCH_SCORE:
                        seen_jobs[jid]["status"] = "rejected"

    all_scored = already_scored_candidates + scored_new
    if not all_scored:
        return []

    all_scored.sort(
        key=lambda j: j.get("match_score", 0),
        reverse=True,
    )

    matched_jobs = [
        j
        for j in all_scored
        if j.get("match_score", 0) >= MIN_MATCH_SCORE
    ]

    run_metadata["high_match_jobs"] = len(matched_jobs)
    run_metadata["high_priority_jobs"] = sum(
        1 for j in matched_jobs if j.get("priority") == "HIGH"
    )

    print()
    print(
        f"Jobs meeting {MIN_MATCH_SCORE}+ match threshold: "
        f"{len(matched_jobs)} (High Priority: {run_metadata['high_priority_jobs']})"
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
    username = os.environ.get("GMAIL_USERNAME") or GMAIL_USERNAME
    app_password = os.environ.get("GMAIL_APP_PASSWORD") or GMAIL_APP_PASSWORD

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

    has_high = any(j.get("priority") == "HIGH" for j in jobs[:MAX_EMAIL_JOBS])
    count = len(jobs[:MAX_EMAIL_JOBS])
    prefix = "🔥 [HIGH PRIORITY ALERT] " if has_high else ""
    message["Subject"] = (
        f"{prefix}AI Job Hunter - {count} High-Match Fresh Jobs "
        f"[{format_ist_and_utc(now_utc)}]"
    )

    lines = [
        "AI JOB HUNTER - MULTI-SOURCE FRESHNESS REPORT",
        "=" * 70,
        "",
        "RUN METADATA:",
        f"  Run Time: {format_ist_and_utc(now_utc)}",
        f"  Scraper Provider: {run_metadata.get('scraper_provider', 'multi-source')}",
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
        (
            f"  High-Priority Fresh Jobs: "
            f"{run_metadata.get('high_priority_jobs', 0)}"
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
                "Next scheduled scan will execute automatically.",
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
            source = str(job.get("source") or "linkedin").upper()
            priority = job.get("priority", "MEDIUM")
            job_id = job.get("_job_id", "N/A")
            canonical_url = job.get("_canonical_url", "")
            apply_url = (
                job.get("applyUrl")
                or job.get("apply_url")
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
                    f"{index}. {title} [{priority} PRIORITY]",
                    f"   Company: {company}",
                    f"   Source: {source}",
                    f"   Location: {location}",
                    "",
                    f"   Posted: {posted_str}",
                    f"   Discovered: {discovered_str}",
                    f"   Detection Latency: {age_str}",
                    f"   Job ID: {job_id}",
                    "",
                    f"   AI MATCH SCORE: {score}/100 ({qualification})",
                    (
                        f"   Fit Breakdown: Tech {tech_fit}/100 | "
                        f"Role {role_fit}/100 | Experience: {exp_fit}"
                    ),
                    "",
                    f"   Apply: {apply_url}",
                    f"   URL: {canonical_url}",
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

        # Update state: mark successfully emailed jobs with notification timestamps
        sent_now = datetime.now(timezone.utc).isoformat()
        for job in jobs[:MAX_EMAIL_JOBS]:
            jid = job.get("_job_id")
            if jid and jid in seen_jobs:
                seen_jobs[jid]["status"] = "emailed"
                seen_jobs[jid]["emailed_at"] = sent_now
                seen_jobs[jid]["notified_at"] = sent_now
                seen_jobs[jid]["match_score"] = job.get("match_score", 0)
                seen_jobs[jid]["priority"] = job.get("priority", "MEDIUM")
                posted_dt = job.get("_posted_at_dt")
                if posted_dt:
                    notif_lat = round((now_utc - posted_dt).total_seconds() / 60.0, 1)
                    seen_jobs[jid]["notification_latency_minutes"] = max(0.0, notif_lat)

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
# MAIN PIPELINE & WORKER LOOP
# ============================================================

def run_pipeline_once(
    state: dict,
    force_all: bool = False,
) -> tuple[list[dict], dict]:
    """
    Executes a single end-to-end acquisition cycle:
    1. Scrapes due sources (LinkedIn Apify, Remote boards, ATS endpoints).
    2. Runs cross-source deduplication and freshness evaluation.
    3. Deterministic pre-filter + Groq batch matching.
    4. Sends immediate Gmail notifications for high-match jobs.
    5. Persists state atomically.
    """
    start_time = datetime.now(timezone.utc)

    run_metadata = {
        "run_start_utc": start_time.isoformat(),
        "run_start_ist": format_ist_and_utc(start_time),
        "run_end_utc": None,
        "run_end_ist": None,
        "scraper_provider": "multi-source",
        "jobs_retrieved": 0,
        "jobs_within_freshness_window": 0,
        "stale_jobs_filtered": 0,
        "duplicates_skipped": 0,
        "email_retries": 0,
        "new_fresh_jobs": 0,
        "eligible_jobs": 0,
        "candidates_surviving_filter": 0,
        "high_match_jobs": 0,
        "high_priority_jobs": 0,
        "emails_sent": 0,
        "email_failures": 0,
        "email_status": "not_attempted",
        "scraper_errors": [],
        "workflow_errors": [],
    }

    matched_jobs = []

    try:
        # 1. Multi-source acquisition
        t0 = time.time()
        raw_jobs = search_jobs(run_metadata, state=state, force_all=force_all)
        t_retrieval = round(time.time() - t0, 2)
        run_metadata["latency_retrieval_sec"] = t_retrieval

        # 2. Freshness & Cross-Source Deduplication
        t1 = time.time()
        eligible_jobs = process_jobs_freshness_and_state(
            raw_jobs,
            state,
            start_time,
            run_metadata,
        )
        t_dedup = round(time.time() - t1, 2)
        run_metadata["latency_dedup_sec"] = t_dedup

        # 3. Local Relevance + AI Batch Matching
        t2 = time.time()
        matched_jobs = score_and_rank_jobs(
            eligible_jobs,
            run_metadata,
            state=state,
        )
        t_matching = round(time.time() - t2, 2)
        run_metadata["latency_matching_sec"] = t_matching

        # 4. Email Notification
        t3 = time.time()
        dry_run = "--dry-run" in sys.argv or os.environ.get("DRY_RUN", "false").lower() == "true"
        if not dry_run:
            print()
            print("=" * 70)
            print("EMAIL REPORT")
            print("=" * 70)
            send_email_report(
                jobs=matched_jobs,
                run_metadata=run_metadata,
                state=state,
            )
        else:
            print()
            print("[DRY RUN] Skipping SMTP email transmission.")
            run_metadata["email_status"] = "dry_run"
        t_email = round(time.time() - t3, 2)
        run_metadata["latency_email_sec"] = t_email

    except Exception as exc:
        print(f"WORKFLOW EXCEPTION: {exc}")
        run_metadata["workflow_errors"].append(str(exc))

    finally:
        end_time = datetime.now(timezone.utc)
        t_total = round((end_time - start_time).total_seconds(), 2)
        run_metadata["latency_total_sec"] = t_total
        run_metadata["run_end_utc"] = end_time.isoformat()
        run_metadata["run_end_ist"] = format_ist_and_utc(end_time)
        state["last_run"] = run_metadata

        # Atomic state persistence
        save_state(state)

        # Production summary logging (Rule 12)
        print_run_summary(run_metadata, start_time, end_time)

    return matched_jobs, run_metadata


def print_run_summary(run_metadata: dict, start_time: datetime, end_time: datetime):
    """
    Format and print structured production execution logs per Rule 12.
    """
    print()
    print("=" * 70)
    print("JOB HUNTER RUN")
    print("=" * 70)
    print(f"Started: {start_time.isoformat()}")
    print()
    print("SOURCE RESULTS")
    print(f"{'Source':<18} {'Status':<12} {'Details'}")
    print("-" * 70)
    for sid, stat in run_metadata.get("source_stats", {}).items():
        name = stat.get("name", sid)
        status = stat.get("status", "N/A")
        raw = stat.get("raw", 0)
        valid = stat.get("valid", 0)
        elapsed = stat.get("elapsed", 0)
        err = stat.get("error", "")
        if err:
            details = f"error: {err[:40]} ({elapsed}s)"
        elif status in ("LIMITED", "UNAVAILABLE"):
            details = f"requires auth / no open feed ({elapsed}s)"
        else:
            details = f"raw={raw:<3} valid={valid:<3} ({elapsed}s)"
        print(f"{name:<18} {status:<12} {details}")

    print()
    print("FILTERING")
    print(f"Discovered:        {run_metadata.get('jobs_retrieved', 0)}")
    print(f"Fresh:             {run_metadata.get('jobs_within_freshness_window', 0)}")
    print(f"Role matched:      {run_metadata.get('candidates_surviving_filter', 0)}")
    print(f"Location matched:  {run_metadata.get('location_matched_jobs', run_metadata.get('candidates_surviving_filter', 0))}")
    print(f"High-match (>=75): {run_metadata.get('high_match_jobs', 0)}")
    print(f"High-priority:     {run_metadata.get('high_priority_jobs', 0)}")
    print(f"Deduplicated:      {run_metadata.get('duplicates_skipped', 0)}")
    print(f"Sent:              {run_metadata.get('emails_sent', 0)}")
    print()
    print("LATENCY")
    print(f"Source retrieval:  {run_metadata.get('latency_retrieval_sec', 0)}s")
    print(f"Matching:          {run_metadata.get('latency_matching_sec', 0)}s")
    print(f"Email:             {run_metadata.get('latency_email_sec', 0)}s")
    print(f"Total:             {run_metadata.get('latency_total_sec', 0)}s")
    print("=" * 70)


def run_worker_loop():
    """
    Near-Real-Time Persistent Worker Daemon.
    Continuously polls fast/medium/slow sources according to their individual intervals.
    Dispatches immediate alerts when high-priority fresh jobs appear.
    """
    print("=" * 70)
    print("AI JOB HUNTER - PERSISTENT WORKER STARTED (NEAR-REAL-TIME MODE)")
    print("Polling sources according to individual intervals (Press Ctrl+C to stop).")
    print("=" * 70)

    state = load_state()
    try:
        while True:
            matched_jobs, meta = run_pipeline_once(state, force_all=False)
            time.sleep(30)
    except KeyboardInterrupt:
        print("\nWorker loop terminated by user.")
    finally:
        save_state(state)


def main():
    """
    Standard single-pass execution used by GitHub Actions and scheduled runs.
    """
    start_time = datetime.now(timezone.utc)
    print("=" * 70)
    print("AI JOB HUNTER PIPELINE STARTED")
    print(f"Start Time: {format_ist_and_utc(start_time)}")
    print("=" * 70)

    state = load_state()
    print(f"Tracked jobs in database: {len(state.get('jobs', {}))}")

    matched_jobs, run_metadata = run_pipeline_once(state, force_all=True)


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print("AI Job Hunter - Multi-Source Real-Time Pipeline")
        print("Usage: python main.py [options]")
        print("Options:")
        print("  --worker, --daemon : Run continuously as a lightweight background polling daemon")
        print("  --dry-run          : Run one acquisition pass without sending emails")
        print("  --help, -h         : Show this help message and exit")
        sys.exit(0)

    if "--worker" in sys.argv or "--daemon" in sys.argv or os.environ.get("RUN_MODE") == "worker":
        run_worker_loop()
    else:
        main()
