import html
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
import traceback
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
    local_score_job,
)
from sources import (
    SourceRegistry,
    normalize_url,
)
from config import (
    SOURCES_CONFIG,
    EXCLUDED_COMPANIES,
    GROQ_BATCH_SIZE,
    MIN_MATCH_SCORE,
    MAX_EMAIL_SAFETY_CEILING,
    MAX_SCRAPED_JOBS,
    HIGH_PRIORITY_SCORE,
    FRESHNESS_WINDOW_MINUTES,
    FRESHNESS_WINDOW_HOURS,
    HIGH_PRIORITY_MAX_AGE_MINUTES,
    TARGET_LOCATIONS as CONFIG_TARGET_LOCATIONS,
    TARGET_ROLE_FAMILIES,
)
from filters import (
    is_company_excluded,
    evaluate_experience_eligibility,
    is_valid_work_location,
    is_role_relevant,
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

# Limits & Batching (No arbitrary truncation)
# Send ALL qualifying jobs. MAX_EMAIL_SAFETY_CEILING is only an extreme runaway ceiling.
MAX_EMAIL_SAFETY_CEILING = int(os.environ.get("MAX_EMAIL_SAFETY_CEILING", "200"))
MAX_SCRAPED_JOBS = int(os.environ.get("MAX_SCRAPED_JOBS", "300"))
MIN_MATCH_SCORE = int(os.environ.get("MIN_MATCH_SCORE", "50"))

# Persistent state management
BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("STATE_DIR", str(BASE_DIR / "state")))
STATE_FILE = STATE_DIR / "seen_jobs.json"
METRICS_FILE = STATE_DIR / "last_run_metrics.json"

# Prevent the state file from growing forever
MAX_SEEN_JOBS = 5000


# ============================================================
# DETERMINISTIC HARD FILTERS (BEFORE GROQ)
# ============================================================

def filter_jobs_by_company(
    jobs: list[dict],
    state: dict | None = None,
    run_metadata: dict | None = None,
) -> list[dict]:
    """
    Hard Company Exclusion Filter:
    Eliminates Infosys, Infosys Limited, Infosys BPM, and subsidiaries BEFORE Groq.
    """
    accepted = []
    rejected_count = 0
    seen_jobs = state.setdefault("jobs", {}) if state is not None else {}

    print()
    print("=" * 70)
    print("EXCLUDED COMPANY FILTER (Infosys and variants)")
    print("=" * 70)

    for job in jobs:
        comp = job.get("companyName") or job.get("company") or ""
        is_exc, reason = is_company_excluded(comp)
        jid = job.get("_job_id")
        title = job.get("title", "Unknown")

        if is_exc:
            rejected_count += 1
            print(f"  [REJECTED COMPANY] {title} @ '{comp}' -> {reason}")
            if jid and jid in seen_jobs:
                seen_jobs[jid]["status"] = "rejected"
                seen_jobs[jid]["reason"] = f"company_excluded: {reason}"
        else:
            accepted.append(job)

    print(
        f"Company filter summary: {len(jobs)} input -> "
        f"{len(accepted)} accepted -> {rejected_count} rejected"
    )
    if run_metadata is not None:
        run_metadata["company_matched_jobs"] = len(accepted)
        run_metadata["company_rejected_jobs"] = rejected_count

    return accepted


def filter_jobs_by_experience(
    jobs: list[dict],
    state: dict | None = None,
    run_metadata: dict | None = None,
) -> list[dict]:
    """
    Deterministic Experience Filter:
    Accepts: fresher, 0-1, 0-2, 1-2 YOE, entry-level, intern, trainee.
    Rejects: 3+, 4+, 5+, 7+, 8+ YOE, Senior, Lead, Principal, Architect BEFORE Groq.
    """
    accepted = []
    rejected_count = 0
    seen_jobs = state.setdefault("jobs", {}) if state is not None else {}

    print()
    print("=" * 70)
    print("EXPERIENCE FILTER (Fresher / Entry-Level / 0-2 YOE only)")
    print("=" * 70)

    for job in jobs:
        is_elig, reason = evaluate_experience_eligibility(job)
        jid = job.get("_job_id")
        title = job.get("title", "Unknown")
        comp = job.get("companyName") or job.get("company") or "Unknown"

        if is_elig:
            accepted.append(job)
        else:
            rejected_count += 1
            print(f"  [REJECTED EXPERIENCE] {title} @ {comp} -> {reason}")
            if jid and jid in seen_jobs:
                seen_jobs[jid]["status"] = "rejected"
                seen_jobs[jid]["reason"] = f"experience_too_high: {reason}"

    print(
        f"Experience filter summary: {len(jobs)} input -> "
        f"{len(accepted)} accepted -> {rejected_count} rejected"
    )
    if run_metadata is not None:
        run_metadata["experience_matched_jobs"] = len(accepted)
        run_metadata["experience_rejected_jobs"] = rejected_count

    return accepted


def filter_jobs_by_strict_location(
    jobs: list[dict],
    state: dict | None = None,
    run_metadata: dict | None = None,
) -> list[dict]:
    """
    Strict Work-Location Filter:
    Accepts ONLY jobs in Mumbai, Hyderabad, Bangalore/Bengaluru, Pune, or Remote.
    """
    accepted = []
    rejected_count = 0
    seen_jobs = state.setdefault("jobs", {}) if state is not None else {}

    print()
    print("=" * 70)
    print("STRICT WORK-LOCATION FILTER (Mumbai, Hyderabad, Bangalore, Pune, Remote)")
    print("=" * 70)

    for job in jobs:
        is_valid, reason = is_valid_work_location(job)
        job_loc = job.get("location", "Unknown")
        title = job.get("title", "Unknown")
        comp = job.get("companyName") or job.get("company") or "Unknown"

        if is_valid:
            accepted.append(job)
            print(f"  [ACCEPTED] {title} @ {comp} | Location: '{job_loc}' -> {reason}")
        else:
            rejected_count += 1
            print(f"  [REJECTED LOCATION] {title} @ {comp} | Location: '{job_loc}' -> {reason}")
            jid = job.get("_job_id")
            if jid and jid in seen_jobs:
                seen_jobs[jid]["status"] = "rejected"
                seen_jobs[jid]["reason"] = f"strict_location_rejected: {reason}"

    print(
        f"Location filter summary: {len(jobs)} input -> "
        f"{len(accepted)} accepted -> {rejected_count} rejected"
    )
    if run_metadata is not None:
        run_metadata["location_matched_jobs"] = len(accepted)
        run_metadata["location_rejected_jobs"] = rejected_count

    return accepted


def filter_jobs_by_role(
    jobs: list[dict],
    state: dict | None = None,
    run_metadata: dict | None = None,
) -> list[dict]:
    """
    Role Relevance Filter:
    Matches all target AI/ML, GenAI, LLM, Agentic, Applied AI, CV, NLP roles.
    Rejects clearly unrelated non-AI roles (Frontend, Java, QA, DevOps, etc.).
    """
    accepted = []
    rejected_count = 0
    seen_jobs = state.setdefault("jobs", {}) if state is not None else {}

    print()
    print("=" * 70)
    print("ROLE RELEVANCE FILTER (AI/ML/GenAI/LLM/Agentic/Applied AI)")
    print("=" * 70)

    for job in jobs:
        title = job.get("title", "")
        desc = job.get("description") or job.get("descriptionHtml") or ""
        is_rel, reason = is_role_relevant(title, desc)
        jid = job.get("_job_id")
        comp = job.get("companyName") or job.get("company") or "Unknown"

        if is_rel:
            accepted.append(job)
        else:
            rejected_count += 1
            source = job.get("source") or job.get("_source_provider") or "unknown"
            if run_metadata is not None:
                run_metadata.setdefault("rejected_titles_by_role", {}).setdefault(source, []).append(title)
            print(f"  [REJECTED ROLE] {title} @ {comp} -> {reason}")
            if jid and jid in seen_jobs:
                seen_jobs[jid]["status"] = "rejected"
                seen_jobs[jid]["reason"] = f"role_not_relevant: {reason}"

    print(
        f"Role filter summary: {len(jobs)} input -> "
        f"{len(accepted)} accepted -> {rejected_count} rejected"
    )
    if run_metadata is not None:
        run_metadata["role_matched_jobs"] = len(accepted)
        run_metadata["role_rejected_jobs"] = rejected_count

    return accepted



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
    Save persistent job state and metrics to disk atomically.
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
        if "last_run" in state and isinstance(state["last_run"], dict):
            state["last_run"]["state_persistence"] = "SUCCESS"
    except Exception as error:
        print(f"ERROR: Could not save state: {error}")
        if "last_run" in state and isinstance(state["last_run"], dict):
            state["last_run"]["state_persistence"] = f"FAILED: {error}"
        raise RuntimeError(f"State persistence failed: {error}") from error


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
    window_minutes: int | None = None,
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
    # If window_minutes is explicitly passed, use it.
    # Otherwise: ATS & remote boards use active posting window (default 48 hours = 2880m).
    # LinkedIn & general fast feeds use sensible 24-hour window (1440m).
    if window_minutes is not None:
        source_window_minutes = window_minutes
    elif source_name in ("greenhouse", "lever", "ashby", "remoteok", "remotive", "workingnomads", "weworkremotely", "nodesk"):
        source_window_minutes = int(os.environ.get("ATS_FRESHNESS_WINDOW_MINUTES", "2880"))
    else:
        source_window_minutes = int(os.environ.get("FRESHNESS_WINDOW_MINUTES", str(FRESHNESS_WINDOW_MINUTES)))

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
    Handles:
    - Synchronous dataset return (HTTP 200 list)
    - Asynchronous / timeout return (HTTP 201 Created with run metadata)
    - Polling in-progress run (RUNNING / READY) until completion
    - Fetching dataset items directly from defaultDatasetId
    - Clean non-secret diagnostic logging
    """
    start_dt = datetime.now(timezone.utc)
    t0 = time.time()

    exec_params = dict(params)
    if "timeout" not in exec_params:
        exec_params["timeout"] = int(os.environ.get("APIFY_TIMEOUT_SECONDS", "120"))

    print()
    print(f"[{label}] Starting Apify invocation at {format_ist_and_utc(start_dt)}...")
    print(f"[{label}] Actor: {ACTOR_ID} | Endpoint: {APIFY_URL}")
    print(f"[{label}] Submitted startUrls: {len(payload.get('startUrls', []))}")

    try:
        response = requests.post(
            APIFY_URL,
            params=exec_params,
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

        # Check for monthly usage hard limit exceeded
        if (
            response.status_code == 403
            or "Monthly usage hard limit exceeded" in response.text
            or "platform-feature-disabled" in response.text
        ):
            print(f"[{label}] Apify monthly usage hard limit exceeded (HTTP 403). Marking source as APIFY_USAGE_LIMITED.")
            run_metadata["apify_usage_limited"] = True

        return [], {
            "status": response.status_code,
            "error": err_msg,
            "duration": duration,
            "actor_run_id": actor_run_id,
            "usage_limited": run_metadata.get("apify_usage_limited", False),
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
    run_status = "UNKNOWN"

    if isinstance(data, list):
        jobs = data
        run_status = "SUCCEEDED"
        print(f"[{label}] Direct dataset list received: {len(jobs)} items.")
    elif isinstance(data, dict):
        for key in ("items", "results"):
            if isinstance(data.get(key), list):
                jobs = data[key]
                run_status = "SUCCEEDED"
                break

        # Check run object
        run_obj = data.get("data") if isinstance(data.get("data"), dict) else data
        run_id = run_obj.get("id") or (actor_run_id if actor_run_id != "Not reported in header" else None)
        run_status = run_obj.get("status", run_status)
        if not dataset_id or dataset_id == "Not reported in header":
            dataset_id = run_obj.get("defaultDatasetId", "Not reported in header")

        # If run is still running or ready, poll until completion
        token = exec_params.get("token", "")
        if run_status in ("RUNNING", "READY") and run_id and token:
            poll_url = f"https://api.apify.com/v2/actor-runs/{run_id}"
            max_wait = int(os.environ.get("APIFY_POLL_TIMEOUT_SECONDS", "60"))
            poll_interval = 5
            elapsed_poll = 0
            print(f"[{label}] Actor run {run_id} is {run_status}. Polling for completion (max {max_wait}s)...")
            while run_status in ("RUNNING", "READY") and elapsed_poll < max_wait:
                time.sleep(poll_interval)
                elapsed_poll += poll_interval
                try:
                    p_res = requests.get(poll_url, params={"token": token}, timeout=15)
                    if p_res.status_code == 200:
                        p_data = p_res.json().get("data", {})
                        run_status = p_data.get("status", run_status)
                        ds_from_poll = p_data.get("defaultDatasetId")
                        if ds_from_poll:
                            dataset_id = ds_from_poll
                        print(f"[{label}] Polled run {run_id}: status={run_status} ({elapsed_poll}s elapsed)")
                        if run_status not in ("RUNNING", "READY"):
                            break
                except Exception as poll_err:
                    print(f"[{label}] Polling warning: {poll_err}")
                    break

        # If jobs still empty and we have a valid dataset_id, fetch items directly
        if not jobs and dataset_id and dataset_id != "Not reported in header" and token:
            dataset_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items"
            print(f"[{label}] Fetching items from Apify dataset {dataset_id}...")
            try:
                ds_res = requests.get(
                    dataset_url,
                    params={"token": token, "clean": "true", "format": "json"},
                    timeout=60,
                )
                if ds_res.status_code == 200:
                    ds_data = ds_res.json()
                    if isinstance(ds_data, list):
                        jobs = ds_data
                    elif isinstance(ds_data, dict) and isinstance(ds_data.get("items"), list):
                        jobs = ds_data["items"]
                    print(f"[{label}] Successfully retrieved {len(jobs)} items from dataset {dataset_id}.")
                else:
                    print(f"[{label}] Dataset items fetch returned HTTP {ds_res.status_code}: {ds_res.text[:300]}")
            except Exception as ds_err:
                print(f"[{label}] Dataset items fetch error: {ds_err}")

    print(f"[{label}] Scraper Diagnostics Summary:")
    print(f"  Actor ID:             {ACTOR_ID}")
    print(f"  HTTP Status:          {response.status_code}")
    print(f"  Actor Run ID:         {actor_run_id}")
    print(f"  Dataset ID:           {dataset_id}")
    print(f"  Actor/Run Status:     {run_status}")
    print(f"  Start URLs Submitted: {len(payload.get('startUrls', []))}")
    print(f"  Jobs Extracted:       {len(jobs)}")

    meta = {
        "label": label,
        "start_utc": start_dt.isoformat(),
        "duration_seconds": round(time.time() - t0, 2),
        "status_code": response.status_code,
        "actor_run_id": actor_run_id,
        "dataset_id": dataset_id,
        "run_status": run_status,
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
        err = "APIFY_API_TOKEN is missing or not set in environment. LinkedIn acquisition could not be live-tested because APIFY_API_TOKEN is unavailable locally."
        print(f"ERROR: {err}")
        run_metadata["scraper_errors"].append(err)
        return []

    params = {"token": APIFY_TOKEN}

    # Targeted multi-partition production search configuration across required cities & AI role families
    payload = {
        "searchQuery": "AI Engineer",
        "searchQueries": [
            "AI Engineer",
            "Machine Learning Engineer",
            "Generative AI Engineer",
            "LLM Engineer",
            "Agentic AI Engineer",
            "AI/ML Engineer",
            "Applied AI Engineer",
            "AI Developer",
            "AI Intern",
            "Machine Learning Intern",
        ],
        "keywords": [
            "AI Engineer",
            "Machine Learning Engineer",
            "Generative AI Engineer",
            "LLM Engineer",
            "Agentic AI Engineer",
            "AI/ML Engineer",
            "Applied AI Engineer",
            "AI Developer",
            "AI Intern",
            "Machine Learning Intern",
        ],
        "locations": [
            "Mumbai",
            "Hyderabad",
            "Bangalore",
            "Bengaluru",
            "Pune",
            "Remote",
        ],
        "location": "India",
        "maxItems": MAX_SCRAPED_JOBS,
        "maxJobs": MAX_SCRAPED_JOBS,
        "jobType": "F",
        "experienceLevel": "2",  # Entry level
        "datePosted": "r86400",  # Past 24 hours max window
        "sortBy": "DD",  # Most Recent
        "scrapeJobDetails": os.environ.get("APIFY_SCRAPE_JOB_DETAILS", "false").lower() == "true",
        "startUrls": [
            {"url": "https://www.linkedin.com/jobs/search/?keywords=AI+Engineer&location=Bengaluru&f_TPR=r86400&sortBy=DD"},
            {"url": "https://www.linkedin.com/jobs/search/?keywords=Machine+Learning+Engineer&location=Bengaluru&f_TPR=r86400&sortBy=DD"},
            {"url": "https://www.linkedin.com/jobs/search/?keywords=Generative+AI+Engineer&location=Bengaluru&f_TPR=r86400&sortBy=DD"},
            {"url": "https://www.linkedin.com/jobs/search/?keywords=LLM+Engineer&location=Bengaluru&f_TPR=r86400&sortBy=DD"},
            {"url": "https://www.linkedin.com/jobs/search/?keywords=AI+Engineer&location=Hyderabad&f_TPR=r86400&sortBy=DD"},
            {"url": "https://www.linkedin.com/jobs/search/?keywords=Machine+Learning+Engineer&location=Hyderabad&f_TPR=r86400&sortBy=DD"},
            {"url": "https://www.linkedin.com/jobs/search/?keywords=Generative+AI+Engineer&location=Hyderabad&f_TPR=r86400&sortBy=DD"},
            {"url": "https://www.linkedin.com/jobs/search/?keywords=AI+Engineer&location=Pune&f_TPR=r86400&sortBy=DD"},
            {"url": "https://www.linkedin.com/jobs/search/?keywords=Machine+Learning+Engineer&location=Pune&f_TPR=r86400&sortBy=DD"},
            {"url": "https://www.linkedin.com/jobs/search/?keywords=Generative+AI+Engineer&location=Pune&f_TPR=r86400&sortBy=DD"},
            {"url": "https://www.linkedin.com/jobs/search/?keywords=AI+Engineer&location=Mumbai&f_TPR=r86400&sortBy=DD"},
            {"url": "https://www.linkedin.com/jobs/search/?keywords=Machine+Learning+Engineer&location=Mumbai&f_TPR=r86400&sortBy=DD"},
            {"url": "https://www.linkedin.com/jobs/search/?keywords=Generative+AI+Engineer&location=Mumbai&f_TPR=r86400&sortBy=DD"},
            {"url": "https://www.linkedin.com/jobs/search/?keywords=AI+Engineer&location=India&f_WT=2&f_TPR=r86400&sortBy=DD"},
            {"url": "https://www.linkedin.com/jobs/search/?keywords=Machine+Learning+Engineer&location=India&f_WT=2&f_TPR=r86400&sortBy=DD"},
            {"url": "https://www.linkedin.com/jobs/search/?keywords=Generative+AI+Engineer&location=India&f_TPR=r86400&sortBy=DD"},
            {"url": "https://www.linkedin.com/jobs/search/?keywords=LLM+Engineer&location=India&f_TPR=r86400&sortBy=DD"},
            {"url": "https://www.linkedin.com/jobs/search/?keywords=Agentic+AI+Engineer&location=India&f_TPR=r86400&sortBy=DD"},
            {"url": "https://www.linkedin.com/jobs/search/?keywords=AI+Intern&location=India&f_TPR=r86400&sortBy=DD"},
            {"url": "https://www.linkedin.com/jobs/search/?keywords=Machine+Learning+Intern&location=India&f_TPR=r86400&sortBy=DD"},
        ],
    }

    print("Search Configuration:")
    print("  sortBy:           'DD' (Most Recent)")
    print("  datePosted:       'r86400' (Past 24 hours)")
    print(f"  startUrls:        {len(payload['startUrls'])} multi-partition URLs (Mumbai, Hyd, Blr, Pune, Remote)")
    print(f"  scrapeJobDetails: {payload['scrapeJobDetails']}")
    print(f"  searchQueries:    {payload['searchQueries']}")

    # --------------------------------------------------------
    # LIVE RUN 1
    # --------------------------------------------------------
    jobs_1, meta_1 = execute_apify_run(payload, params, "Run 1", run_metadata)

    # If startUrls returned 0 items, run fallback direct query to ensure resilience
    # (Skip if Apify account monthly usage hard limit was reached to prevent redundant failed requests)
    if not jobs_1:
        if run_metadata.get("apify_usage_limited"):
            print("[APIFY FALLBACK] Skipped: Apify account monthly usage hard limit reached (HTTP 403).")
        else:
            print()
            print("[APIFY FALLBACK] startUrls yielded 0 jobs. Running direct search queries fallback...")
            fallback_payload = {
                "searchQuery": "AI Engineer",
                "searchQueries": [
                    "AI Engineer",
                    "Machine Learning Engineer",
                    "Generative AI Engineer",
                    "LLM Engineer",
                    "AI Intern",
                ],
                "locations": ["Bangalore", "Hyderabad", "Pune", "Mumbai", "Remote"],
                "location": "India",
                "maxItems": 50,
                "maxJobs": 50,
                "datePosted": "r86400",
                "sortBy": "DD",
                "scrapeJobDetails": False,
            }
            jobs_fb, meta_fb = execute_apify_run(fallback_payload, params, "Fallback", run_metadata)
            if jobs_fb:
                jobs_1 = jobs_fb
                meta_1 = meta_fb

    summary_1 = inspect_and_log_jobs(
        jobs_1,
        datetime.now(timezone.utc),
        "Run 1",
    )

    # --------------------------------------------------------
    # RECORD LINKEDIN METRICS
    # --------------------------------------------------------
    fresh_li_count = 0
    now_utc = datetime.now(timezone.utc)
    for j in jobs_1:
        fr = calculate_freshness(j, now_utc)
        if fr.get("is_fresh"):
            fresh_li_count += 1

    run_metadata["linkedin_metrics"] = {
        "partitions_submitted": len(payload.get("startUrls", [])),
        "partitions_successful": len(payload.get("startUrls", [])) if jobs_1 else 0,
        "partitions_failed": 0 if jobs_1 else len(payload.get("startUrls", [])),
        "total_items_returned": len(jobs_1),
        "total_normalized_jobs": len(jobs_1),
        "total_fresh_jobs": fresh_li_count,
    }

    # --------------------------------------------------------
    # PAUSE & LIVE RUN 2 (COMPARISON TEST)
    # --------------------------------------------------------
    enable_compare = os.environ.get("APIFY_COMPARE_RUNS", "false").lower() == "true"
    if enable_compare and not run_metadata.get("apify_usage_limited"):
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
            if run_metadata.get("apify_usage_limited"):
                status = "APIFY_USAGE_LIMITED"
            elif raw:
                status = "OK"
            else:
                status = "EMPTY"
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
    source_funnel = run_metadata.setdefault("source_funnel", {})

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

        sf = source_funnel.setdefault(source_name, {"raw": 0, "stale": 0, "duplicate": 0, "eligible": 0})
        sf["raw"] += 1

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

        # Deduplication check:
        # - "emailed": permanently skip to avoid repeat alert spam!
        # - "email_abandoned": permanently skip after exceeding max retry attempts.
        # - "email_failed", "qualified", "discovered", and recent "rejected": retain eligibility!
        if existing_record and existing_record.get("status") in (
            "emailed",
            "email_abandoned",
        ):
            run_metadata["duplicates_skipped"] += 1
            sf["duplicate"] += 1
            existing_record["last_seen"] = discovered_at.isoformat()
            seen_sources = existing_record.setdefault("seen_sources", [existing_record.get("source", source_name)])
            if source_name not in seen_sources:
                seen_sources.append(source_name)
            continue

        # Check retry limit for failed email attempts (max 3 retries)
        if existing_record and existing_record.get("status") == "email_failed":
            attempts = existing_record.get("email_attempts", 0)
            if attempts >= 3:
                existing_record["status"] = "email_abandoned"
                run_metadata["duplicates_skipped"] += 1
                sf["duplicate"] += 1
                print(
                    f"[RETRY ABANDONED] Max email attempts (3) exceeded for "
                    f"'{job.get('title')}' at '{job.get('companyName') or job.get('company')}'"
                )
                continue

        # Check for truly obsolete listings (older than max_age_days, default 14 days)
        max_age_days = int(os.environ.get("MAX_JOB_AGE_DAYS", "14"))
        latency_mins = freshness.get("discovery_latency_minutes")
        if latency_mins is not None and latency_mins > (max_age_days * 1440):
            run_metadata["stale_jobs_filtered"] += 1
            sf["stale"] += 1
            print(
                f"[EXPIRED LISTING] '{job.get('title')}' at "
                f"'{job.get('companyName') or job.get('company')}' - "
                f"posted {round(latency_mins / 1440, 1)} days ago (exceeds {max_age_days}d limit)"
            )
            continue

        # Track fresh jobs count
        if freshness["is_fresh"]:
            run_metadata["jobs_within_freshness_window"] += 1

        # 3. Handle Retry vs Re-evaluating vs New Discovery
        if existing_record:
            prev_status = existing_record.get("status", "discovered")
            existing_record["last_seen"] = discovered_at.isoformat()
            seen_sources = existing_record.setdefault("seen_sources", [existing_record.get("source", source_name)])
            if source_name not in seen_sources:
                seen_sources.append(source_name)

            if prev_status == "email_failed":
                print(
                    f"[RETRY ELIGIBLE] '{job.get('title')}' at "
                    f"'{job.get('companyName') or job.get('company')}' (Prior status: email_failed)"
                )
                run_metadata["email_retries"] += 1
            elif prev_status == "qualified":
                print(
                    f"[QUALIFIED RETRY] '{job.get('title')}' at "
                    f"'{job.get('companyName') or job.get('company')}' (Prior status: qualified)"
                )
                run_metadata["email_retries"] += 1
            elif prev_status == "rejected":
                print(
                    f"[RE-EVALUATING] '{job.get('title')}' at "
                    f"'{job.get('companyName') or job.get('company')}' (Previously marked rejected)"
                )
            else:
                print(
                    f"[DISCOVERED RETRY] '{job.get('title')}' at "
                    f"'{job.get('companyName') or job.get('company')}'"
                )

            # Only reuse prior match score if the job was ALREADY qualified and waiting for email retry
            # Never bypass AI matching on re-evaluating rejected or un-scored discovered candidates!
            if prev_status in ("qualified", "email_failed") and existing_record.get("match_score", 0) >= MIN_MATCH_SCORE:
                job["match_score"] = existing_record.get("match_score")
                job["qualification"] = existing_record.get("qualification", "GOOD_MATCH")
                job["priority"] = existing_record.get("priority", "HIGH")
                job["matcher_path"] = existing_record.get("matcher_path", "CACHED")
                job["groq_status"] = existing_record.get("groq_status", "PREVIOUSLY_QUALIFIED")
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

        sf["eligible"] += 1
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
    Batched AI/Groq matching (or deterministic local fallback).
    Evaluates ALL candidate jobs in batches of GROQ_BATCH_SIZE (15) without truncation.
    Filters by MIN_MATCH_SCORE (50) floor.
    Records match outcomes and rejection states in state to prevent redundant Groq calls.
    """
    if not jobs:
        print("No jobs available for matching.")
        return []

    candidates = jobs
    run_metadata["candidates_surviving_filter"] = len(candidates)
    seen_jobs = state.setdefault("jobs", {}) if state is not None else {}

    # Separate candidates: new un-scored vs previously evaluated retries
    unscored_candidates = [j for j in candidates if not j.get("_already_scored")]
    already_scored_candidates = [j for j in candidates if j.get("_already_scored")]

    for j in already_scored_candidates:
        title = j.get("title", "Unknown")
        comp = j.get("companyName") or j.get("company") or "Unknown"
        print(f"  [PRE-QUALIFIED RETRY] '{title}' @ '{comp}' (Score: {j.get('match_score')}, Path: CACHED)")

    scored_new = []
    if unscored_candidates:
        print()
        print("=" * 70)
        print(f"AI BATCH MATCHING (GROQ / LOCAL FALLBACK) - {len(unscored_candidates)} candidates")
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
                    seen_jobs[jid]["matcher_path"] = j.get("matcher_path", "LOCAL")
                    seen_jobs[jid]["groq_status"] = j.get("groq_status", "UNKNOWN")
                    if score < MIN_MATCH_SCORE:
                        seen_jobs[jid]["status"] = "rejected"
                        seen_jobs[jid]["reason"] = f"score_below_threshold: {score} < {MIN_MATCH_SCORE}"

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

    # TASK D: Exact Matcher Diagnostics per Candidate (Non-sensitive)
    print()
    print("=" * 70)
    print("MATCHER CANDIDATE DIAGNOSTICS")
    print("=" * 70)
    for j in all_scored:
        c_title = j.get("title", "Unknown")
        c_comp = j.get("companyName") or j.get("company") or "Unknown"
        c_loc = j.get("location", "Unknown")
        c_path = j.get("matcher_path", "LOCAL")
        c_groq = j.get("groq_status", "N/A")
        c_local_score = j.get("local_score", "N/A")
        c_score = j.get("match_score", 0)
        c_is_match = c_score >= MIN_MATCH_SCORE
        c_decision = "MATCHED" if c_is_match else "REJECTED"
        c_rej_reason = "N/A (Met threshold)" if c_is_match else f"Score {c_score} < {MIN_MATCH_SCORE} threshold"

        print(
            f"  [CANDIDATE] '{c_title}' @ '{c_comp}'\n"
            f"              Location:        '{c_loc}'\n"
            f"              Matcher Path:    {c_path}\n"
            f"              Groq Status:     {c_groq}\n"
            f"              Local Score:     {c_local_score}\n"
            f"              Final Decision:  {c_decision} (Score: {c_score}/100)\n"
            f"              Rejection Reason:{c_rej_reason}"
        )

    print()
    print(
        f"Jobs meeting {MIN_MATCH_SCORE}+ match threshold: "
        f"{len(matched_jobs)} (High Priority: {run_metadata['high_priority_jobs']})"
    )

    return matched_jobs


# ============================================================
# EMAIL DIGEST GENERATION & REPORTING
# ============================================================

def format_source_name(source: str | None) -> str:
    """
    Format concise, human-readable source names.
    Examples: LinkedIn, Greenhouse, Ashby, Lever, Remote OK, Remotive, etc.
    """
    s = str(source or "").lower().strip()
    sources_map = {
        "linkedin": "LinkedIn",
        "greenhouse": "Greenhouse",
        "ashby": "Ashby",
        "lever": "Lever",
        "remoteok": "Remote OK",
        "remotive": "Remotive",
        "weworkremotely": "We Work Remotely",
        "workingnomads": "Working Nomads",
        "nodesk": "NoDesk",
        "indeed": "Indeed",
        "wellfound": "Wellfound",
        "naukri": "Naukri",
        "instahyre": "Instahyre",
        "cutshort": "Cutshort",
        "foundit": "Foundit",
        "hirist": "Hirist",
    }
    return sources_map.get(s, s.title() if s else "Direct")


def get_single_job_location_name(job: dict) -> str:
    """
    Extract a concise city or remote indicator for single-job email subject lines.
    """
    loc = str(job.get("location") or "").lower()
    wp = str(
        job.get("workplace")
        or job.get("workplaceType")
        or job.get("remote_type")
        or ""
    ).lower()
    if "pune" in loc:
        return "Pune"
    if "mumbai" in loc or "bombay" in loc:
        return "Mumbai"
    if "hyderabad" in loc or "secunderabad" in loc:
        return "Hyderabad"
    if "bengaluru" in loc or "bangalore" in loc:
        return "Bangalore"
    if "remote" in loc or "remote" in wp:
        return "Remote"
    return "India"


def extract_key_skills(job: dict) -> list[str]:
    """
    Extract clean, concise technical skill keywords for display.
    """
    skills = []
    seen = set()

    def add_skill(name: str):
        name_clean = name.strip()
        if not name_clean:
            return
        acronyms = {"ai", "ml", "llm", "llms", "rag", "nlp", "gpu", "mlops", "api"}
        if name_clean.lower() in acronyms:
            formatted = name_clean.upper() if name_clean.lower() != "llms" else "LLMs"
        elif name_clean.lower() == "fastapi":
            formatted = "FastAPI"
        elif name_clean.lower() == "pytorch":
            formatted = "PyTorch"
        elif name_clean.lower() == "tensorflow":
            formatted = "TensorFlow"
        elif name_clean.lower() == "langchain":
            formatted = "LangChain"
        elif name_clean.lower() == "langgraph":
            formatted = "LangGraph"
        elif name_clean.lower() == "scikit-learn":
            formatted = "scikit-learn"
        else:
            formatted = name_clean.title()

        if formatted.lower() not in seen:
            seen.add(formatted.lower())
            skills.append(formatted)

    # 1. From technical_matches
    for tm in job.get("technical_matches") or []:
        add_skill(tm)

    # 2. From key_matches (if short keyword tokens)
    for km in job.get("key_matches") or []:
        km_clean = str(km).replace("+", "").strip()
        if km_clean and len(km_clean.split()) <= 3:
            add_skill(km_clean)

    # 3. From text overlap with core AI/ML tech stack
    text = (str(job.get("title") or "") + " " + str(job.get("description") or "")).lower()
    core_techs = [
        "Python", "Machine Learning", "PyTorch", "TensorFlow", "LLMs", "Generative AI",
        "RAG", "FastAPI", "Docker", "Agentic AI", "LangChain", "Vector Databases",
        "MLOps", "Qdrant", "Pinecone", "Deep Learning",
    ]
    for tech in core_techs:
        if tech.lower() in text and tech.lower() not in seen:
            add_skill(tech)

    return skills[:8] if skills else ["Python", "Machine Learning", "LLMs", "FastAPI"]


def format_why_it_matches(job: dict) -> str:
    """
    Produce a concise 1-2 sentence explanation of why the role matches.
    Never outputs raw debug logs, code fences, or entire LLM reasoning blocks.
    """
    raw = job.get("match_reason") or job.get("reason") or ""
    raw = re.sub(r"^(why it matches:?\s*)+", "", raw, flags=re.IGNORECASE).strip()
    if not raw:
        return "The role focuses on ML/AI development matching your AI/ML background and technical skills."

    # Split into sentences and take first 1 or 2
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", raw) if s.strip()]
    if len(sentences) >= 2:
        short = f"{sentences[0]} {sentences[1]}"
    elif sentences:
        short = sentences[0]
    else:
        short = raw

    if len(short) > 280:
        short = short[:277].rsplit(" ", 1)[0] + "..."
    return short


def format_job_age(job: dict) -> str:
    """
    Format human-readable relative time (e.g. '39 minutes ago' or '1 hour ago').
    """
    raw_age = job.get("_latency_formatted") or ""
    if not raw_age or "Unknown" in raw_age:
        posted_dt = job.get("_posted_at_dt")
        if posted_dt:
            mins = max(0.0, (datetime.now(timezone.utc) - posted_dt).total_seconds() / 60.0)
            if mins < 1:
                return "< 1 minute ago"
            elif mins < 60:
                return f"{int(round(mins))} minutes ago"
            elif mins < 1440:
                hours = round(mins / 60.0, 1)
                h_str = f"{int(hours)}" if hours.is_integer() else f"{hours}"
                return f"{h_str} hour{'s' if hours != 1 else ''} ago"
            else:
                days = round(mins / 1440.0, 1)
                d_str = f"{int(days)}" if days.is_integer() else f"{days}"
                return f"{d_str} day{'s' if days != 1 else ''} ago"
        return "recently"

    if raw_age.lower().endswith("ago"):
        return raw_age
    if raw_age.startswith("<"):
        return f"{raw_age} ago"
    return f"{raw_age} ago"


def build_email_digest(
    jobs: list[dict],
    now_utc: datetime | None = None,
) -> tuple[str, str, str]:
    """
    Builds the clean Job Alert Digest email.
    Returns: (subject, html_content, text_content)

    Features:
    - Clean card-based structure per job
    - Shows ONLY: Title, Company, 📍 Location, 🕐 Posted, Source, Match score, Key skills, Why it matches, Apply button
    - Single Apply button/link per job pointing directly to destination
    - Zero tel: links, zero raw URLs, zero internal IDs, zero scraper/pipeline diagnostics
    - No misleading fake 0/100 score breakdowns
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    count = len(jobs)
    if count == 1:
        loc_name = get_single_job_location_name(jobs[0])
        subject = f"AI Job Hunter | 1 New AI Match | {loc_name}"
    else:
        subject = f"AI Job Hunter | {count} New Matches | Mumbai · Hyderabad · Bangalore · Pune · Remote"

    match_word = "match" if count == 1 else "matches"

    cards_html = []
    text_cards = []

    for index, job in enumerate(jobs, start=1):
        title = html.escape(str(job.get("title") or "Unknown Title"))
        comp = html.escape(str(job.get("companyName") or job.get("company") or "Unknown Company"))
        loc = html.escape(str(job.get("location") or "India"))
        source_name = format_source_name(job.get("source"))
        source_html = html.escape(source_name)
        score = int(job.get("match_score", 0))
        age_str = format_job_age(job)
        why_matches = format_why_it_matches(job)
        why_matches_html = html.escape(why_matches)
        skills = extract_key_skills(job)
        skills_str = " · ".join(skills)
        skills_str_html = html.escape(skills_str)

        apply_url = (
            job.get("apply_url")
            or job.get("applyUrl")
            or job.get("url")
            or job.get("_canonical_url")
            or "#"
        )
        safe_apply_url = html.escape(apply_url)

        # HTML Card
        card_html = f"""    <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 22px 24px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.04);">
      <div style="margin-bottom: 12px;">
        <h2 style="margin: 0 0 4px 0; font-size: 18px; font-weight: 700; color: #0f172a; line-height: 1.3;">
          {title}
        </h2>
        <div style="font-size: 15px; font-weight: 600; color: #334155;">
          {comp}
        </div>
      </div>

      <div style="font-size: 13px; color: #64748b; line-height: 1.6; margin-bottom: 14px;">
        <div style="margin-bottom: 3px;">
          <span style="color: #475569;">📍</span> {loc}
        </div>
        <div style="margin-bottom: 3px;">
          <span style="color: #475569;">🕐</span> Posted {html.escape(age_str)}
        </div>
        <div style="margin-bottom: 3px;">
          Source: <strong style="color: #334155;">{source_html}</strong>
        </div>
      </div>

      <div style="margin-bottom: 14px;">
        <span style="display: inline-block; background-color: #ecfdf5; color: #047857; font-weight: 700; font-size: 13px; padding: 4px 10px; border-radius: 6px; border: 1px solid #a7f3d0;">
          Match: {score}/100
        </span>
      </div>

      <div style="font-size: 13px; color: #334155; line-height: 1.5; margin-bottom: 14px;">
        <strong>Key skills:</strong> {skills_str_html}
      </div>

      <div style="font-size: 13px; color: #475569; line-height: 1.5; margin-bottom: 18px; padding-left: 12px; border-left: 3px solid #cbd5e1;">
        <strong style="color: #1e293b;">Why it matches:</strong><br>
        {why_matches_html}
      </div>

      <div>
        <a href="{safe_apply_url}" target="_blank" rel="noopener noreferrer" style="display: inline-block; background-color: #0f172a; color: #ffffff; text-decoration: none; font-size: 13px; font-weight: 600; padding: 10px 22px; border-radius: 6px; letter-spacing: 0.01em;">
          Apply &rarr;
        </a>
      </div>
    </div>"""
        cards_html.append(card_html)

        # Plain Text Card
        text_card = f"""{job.get('title', 'Unknown Title')}
{job.get('companyName') or job.get('company', 'Unknown Company')}

📍 {job.get('location', 'India')}
🕐 Posted {age_str}
Source: {source_name}

Match: {score}/100

Key skills: {skills_str}

Why it matches:
{why_matches}

[ Apply: {apply_url} ]"""
        text_cards.append(text_card)

    cards_joined = "\n\n".join(cards_html)
    separator = "─" * 60

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="format-detection" content="telephone=no">
  <title>{html.escape(subject)}</title>
</head>
<body style="margin: 0; padding: 0; background-color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased; color: #0f172a;">
  <div style="max-width: 600px; margin: 0 auto; padding: 32px 16px;">
    <div style="margin-bottom: 28px; text-align: left;">
      <h1 style="margin: 0 0 6px 0; font-size: 22px; font-weight: 800; letter-spacing: -0.02em; color: #0f172a;">
        AI JOB HUNTER
      </h1>
      <p style="margin: 0; font-size: 15px; color: #64748b; font-weight: 500;">
        {count} new {match_word} matching your profile
      </p>
    </div>

{cards_joined}

    <div style="margin-top: 36px; padding-top: 20px; border-top: 1px solid #e2e8f0; text-align: center; color: #94a3b8; font-size: 13px; line-height: 1.6;">
      <div style="font-weight: 600; color: #64748b; margin-bottom: 4px;">AI Job Hunter</div>
      <div>Monitoring Mumbai &middot; Hyderabad &middot; Bangalore &middot; Pune &middot; Remote</div>
    </div>
  </div>
</body>
</html>"""

    text_content = f"""AI JOB HUNTER
{count} new {match_word} for you

{separator}

""" + f"\n\n{separator}\n\n".join(text_cards) + f"""

{separator}

AI Job Hunter
Monitoring Mumbai · Hyderabad · Bangalore · Pune · Remote"""

    return subject, html_content, text_content


def send_email_report(
    jobs: list[dict],
    run_metadata: dict,
    state: dict,
):
    """
    Send clean Job Alert Digest via Gmail SMTP.
    - Only sends if qualifying jobs exist
    - Clean, human-readable HTML digest + plain text fallback
    - Sends ALL qualifying jobs without arbitrary truncation
      (MAX_EMAIL_SAFETY_CEILING is only an extreme runaway ceiling)
    - Atomic status transition: sets status='emailed' upon success;
      sets status='email_failed' on error so jobs remain eligible for retry!
    """
    username = os.environ.get("GMAIL_USERNAME") or GMAIL_USERNAME
    app_password = os.environ.get("GMAIL_APP_PASSWORD") or GMAIL_APP_PASSWORD

    now_utc = datetime.now(timezone.utc)
    run_metadata["run_end_utc"] = now_utc.isoformat()
    run_metadata["run_end_ist"] = format_ist_and_utc(now_utc)

    if not jobs:
        print("No matching jobs to send. Skipping email delivery.")
        run_metadata["emails_sent"] = 0
        run_metadata["email_status"] = "skipped_empty"
        return

    if not username or not app_password:
        print(
            "WARNING: Gmail credentials not configured. "
            "Skipping email delivery."
        )
        return

    if len(jobs) > MAX_EMAIL_SAFETY_CEILING:
        print(
            f"WARNING: Jobs count ({len(jobs)}) exceeds safety ceiling "
            f"({MAX_EMAIL_SAFETY_CEILING}). Capping to safety ceiling."
        )
        jobs_to_send = jobs[:MAX_EMAIL_SAFETY_CEILING]
    else:
        jobs_to_send = jobs

    subject, html_content, text_content = build_email_digest(
        jobs_to_send,
        now_utc=now_utc,
    )

    message = EmailMessage()
    message["From"] = username
    message["To"] = username
    message["Subject"] = subject
    message.set_content(text_content)
    message.add_alternative(html_content, subtype="html")

    print()
    print(f"Connecting to Gmail SMTP (smtp.gmail.com:465) to deliver {len(jobs_to_send)} qualifying jobs...")

    seen_jobs = state.setdefault("jobs", {})

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(username, app_password)
            server.send_message(message)

        print(f"Email sent successfully! ({len(jobs_to_send)} jobs delivered)")
        run_metadata["emails_sent"] = len(jobs_to_send)
        run_metadata["email_status"] = "success"

        # Update state: mark successfully emailed jobs with notification timestamps
        sent_now = datetime.now(timezone.utc).isoformat()
        for job in jobs_to_send:
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
        run_metadata["email_failures"] = len(jobs_to_send)

        # Mark jobs as email_failed so they remain eligible for retry next run!
        for job in jobs_to_send:
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
    1. Multi-source acquisition (LinkedIn Apify, Remote boards, ATS endpoints).
    2. Freshness & Cross-Source Deduplication.
    3. Hard Company Filter (Reject Infosys and variants BEFORE Groq).
    4. Deterministic Experience Filter (Fresher / 0-2 YOE only BEFORE Groq).
    5. Strict Work-Location Filter (Mumbai, Hyderabad, Bangalore, Pune, Remote).
    6. Role Relevance Filter (AI/ML/GenAI/LLM/Agentic/Applied AI).
    7. Batched AI/Groq Matching (or deterministic local fallback) in batches of 15.
    8. Sends immediate Gmail notifications for ALL qualifying jobs (no arbitrary cap).
    9. Persists state atomically.
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
        "company_matched_jobs": 0,
        "company_rejected_jobs": 0,
        "experience_matched_jobs": 0,
        "experience_rejected_jobs": 0,
        "location_matched_jobs": 0,
        "location_rejected_jobs": 0,
        "role_matched_jobs": 0,
        "role_rejected_jobs": 0,
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

        # 3. Hard Company Filter (Reject Infosys and variants BEFORE Groq)
        t_comp = time.time()
        company_passed_jobs = filter_jobs_by_company(
            eligible_jobs,
            state=state,
            run_metadata=run_metadata,
        )
        run_metadata["latency_company_sec"] = round(time.time() - t_comp, 2)

        # 4. Deterministic Experience Filter (Fresher / 0-2 YOE only BEFORE Groq)
        t_exp = time.time()
        exp_passed_jobs = filter_jobs_by_experience(
            company_passed_jobs,
            state=state,
            run_metadata=run_metadata,
        )
        run_metadata["latency_experience_sec"] = round(time.time() - t_exp, 2)

        # 5. Strict Work-Location Filter (Mumbai, Hyderabad, Bangalore, Pune, Remote)
        t_loc = time.time()
        location_passed_jobs = filter_jobs_by_strict_location(
            exp_passed_jobs,
            state=state,
            run_metadata=run_metadata,
        )
        run_metadata["latency_location_sec"] = round(time.time() - t_loc, 2)

        # 6. Role Relevance Filter (AI/ML/GenAI/LLM/Agentic/Applied AI)
        t_role = time.time()
        role_passed_jobs = filter_jobs_by_role(
            location_passed_jobs,
            state=state,
            run_metadata=run_metadata,
        )
        run_metadata["latency_role_sec"] = round(time.time() - t_role, 2)

        # 7. Batched AI/Groq Matching (or local fallback)
        t2 = time.time()
        matched_jobs = score_and_rank_jobs(
            role_passed_jobs,
            run_metadata,
            state=state,
        )
        t_matching = round(time.time() - t2, 2)
        run_metadata["latency_matching_sec"] = t_matching

        # 8. Email Notification (Sends ALL qualifying jobs)
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

        # Track per-source diagnostic funnel metrics (RAW, NORMALIZED, NEW, COMPANY, EXPERIENCE, LOCATION, ROLE, FINAL ELIGIBLE)
        sources_seen = set()
        for j in raw_jobs:
            s = j.get("source") or j.get("_source_provider") or "unknown"
            sources_seen.add(s)
        for sid in run_metadata.get("source_stats", {}).keys():
            sources_seen.add(sid)

        per_source = {}
        for s in sources_seen:
            per_source[s] = {
                "raw": sum(1 for j in raw_jobs if (j.get("source") or j.get("_source_provider")) == s),
                "normalized": sum(1 for j in raw_jobs if (j.get("source") or j.get("_source_provider")) == s),
                "new": sum(1 for j in eligible_jobs if (j.get("source") or j.get("_source_provider")) == s),
                "company_pass": sum(1 for j in company_passed_jobs if (j.get("source") or j.get("_source_provider")) == s),
                "experience_pass": sum(1 for j in exp_passed_jobs if (j.get("source") or j.get("_source_provider")) == s),
                "location_pass": sum(1 for j in location_passed_jobs if (j.get("source") or j.get("_source_provider")) == s),
                "role_pass": sum(1 for j in role_passed_jobs if (j.get("source") or j.get("_source_provider")) == s),
                "final_eligible": sum(1 for j in matched_jobs if (j.get("source") or j.get("_source_provider")) == s),
            }
        run_metadata["per_source_funnel"] = per_source

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
        try:
            save_state(state)
            run_metadata["state_persistence"] = "SUCCESS"
        except Exception as state_err:
            run_metadata["state_persistence"] = f"FAILED: {state_err}"
            run_metadata["workflow_errors"].append(f"State persistence failure: {state_err}")
            print(f"FATAL: State persistence failed: {state_err}")

        # Production summary logging & telemetry funnel
        print_run_summary(run_metadata, start_time, end_time)

    return matched_jobs, run_metadata


def print_run_summary(run_metadata: dict, start_time: datetime, end_time: datetime):
    """
    Format and print structured production execution logs and concise funnel telemetry.
    Never prints API keys, Gmail app passwords, or other sensitive secrets.
    """
    raw_count = run_metadata.get("jobs_retrieved", 0)
    norm_count = raw_count
    stale_count = run_metadata.get("stale_jobs_filtered", 0)
    new_count = run_metadata.get("eligible_jobs", 0)
    comp_count = run_metadata.get("company_matched_jobs", 0)
    exp_count = run_metadata.get("experience_matched_jobs", 0)
    loc_count = run_metadata.get("location_matched_jobs", 0)
    role_count = run_metadata.get("role_matched_jobs", 0)
    candidates_count = run_metadata.get("candidates_surviving_filter", 0)
    matched_count = run_metadata.get("high_match_jobs", 0)
    final_count = matched_count
    emailed_count = run_metadata.get("emails_sent", 0)
    email_failed_count = run_metadata.get("email_failures", 0)

    dedup_count = run_metadata.get("duplicates_skipped", 0)
    comp_rejected = run_metadata.get("company_rejected_jobs", 0)
    exp_rejected = run_metadata.get("experience_rejected_jobs", 0)
    loc_rejected = run_metadata.get("location_rejected_jobs", 0)
    role_rejected = run_metadata.get("role_rejected_jobs", 0)

    source_stats = run_metadata.get("source_stats", {})
    li_count = source_stats.get("linkedin", {}).get("raw", 0)
    non_li_count = sum(
        stat.get("raw", 0) for sid, stat in source_stats.items() if sid != "linkedin"
    )
    state_persist = run_metadata.get("state_persistence", "SUCCESS")

    print()
    print("=" * 80)
    print("JOB HUNTER RUN SUMMARY & TELEMETRY FUNNEL")
    print("=" * 80)
    print(f"Run Started (UTC): {start_time.isoformat()}")
    print(f"Run Started (IST): {format_ist_and_utc(start_time)}")
    print(f"Run Ended (UTC):   {end_time.isoformat()}")
    print(f"Run Ended (IST):   {format_ist_and_utc(end_time)}")
    print(f"Total Duration:    {run_metadata.get('latency_total_sec', 0)}s")
    print()
    print("-" * 80)
    print("CONCISE FILTERING FUNNEL")
    print("-" * 80)
    print(f"  RAW:                 {raw_count}")
    print(f"  NORMALIZED:          {norm_count}")
    print(f"  STALE:               {stale_count}")
    print(f"  NEW:                 {new_count}")
    print(f"  DEDUPLICATED:        {dedup_count}")
    print(f"  COMPANY_REJECTED:    {comp_rejected}")
    print(f"  EXPERIENCE_REJECTED: {exp_rejected}")
    print(f"  LOCATION_REJECTED:   {loc_rejected}")
    print(f"  ROLE_REJECTED:       {role_rejected}")
    print(f"  MATCH_CANDIDATES:    {candidates_count}")
    print(f"  MATCHED:             {matched_count}")
    print(f"  EMAILED:             {emailed_count}")
    print(f"  EMAIL_FAILED:        {email_failed_count}")
    print()
    print(
        f"Summary: RAW={raw_count} -> NORM={norm_count} -> STALE={stale_count} -> "
        f"NEW={new_count} -> DEDUP={dedup_count} -> COMP={comp_count} -> EXP={exp_count} -> "
        f"LOC={loc_count} -> ROLE={role_count} -> CANDIDATES={candidates_count} -> "
        f"MATCHED={matched_count} -> EMAILED={emailed_count} (EMAIL_FAILED={email_failed_count})"
    )

    li_metrics = run_metadata.get("linkedin_metrics", {})
    if li_metrics:
        print()
        print("-" * 80)
        print("LINKEDIN ACQUISITION METRICS")
        print("-" * 80)
        print(f"  Partitions submitted:   {li_metrics.get('partitions_submitted', 0)}")
        print(f"  Partitions successful:  {li_metrics.get('partitions_successful', 0)}")
        print(f"  Partitions failed:      {li_metrics.get('partitions_failed', 0)}")
        print(f"  Total items returned:   {li_metrics.get('total_items_returned', 0)}")
        print(f"  Total normalized jobs:  {li_metrics.get('total_normalized_jobs', 0)}")
        print(f"  Total fresh jobs:       {li_metrics.get('total_fresh_jobs', 0)}")

    print()
    print("-" * 80)
    print("SOURCE COUNTS & BREAKDOWN")
    print("-" * 80)
    print(f"Total Sources Queried:        {len(source_stats)}")
    print(f"LinkedIn Jobs Acquired:       {li_count}")
    print(f"Non-LinkedIn Jobs Acquired:   {non_li_count}")
    print()
    print(f"{'Source':<18} {'Status':<12} {'Raw':<6} {'Valid':<6} {'Details'}")
    print("-" * 80)
    for sid, stat in sorted(source_stats.items()):
        name = stat.get("name", sid)
        status = stat.get("status", "N/A")
        raw = stat.get("raw", 0)
        valid = stat.get("valid", 0)
        elapsed = stat.get("elapsed", 0)
        err = stat.get("error", "")
        if err:
            details = f"error: {err[:40]} ({elapsed}s)"
        elif status == "APIFY_USAGE_LIMITED":
            details = f"monthly usage limit exceeded ({elapsed}s)"
        elif status in ("LIMITED", "UNAVAILABLE"):
            details = f"requires auth / no open feed ({elapsed}s)"
        else:
            details = f"fetched in {elapsed}s"
        print(f"{name:<18} {status:<12} {raw:<6} {valid:<6} {details}")

    print()
    print("-" * 80)
    print("PER-SOURCE BREAKDOWN & ELIGIBILITY")
    print("-" * 80)
    print(f"{'SOURCE':<18} | {'RAW':<6} | {'STALE':<6} | {'DUPLICATE':<10} | {'ELIGIBLE':<8}")
    print("-" * 80)
    source_funnel = run_metadata.get("source_funnel", {})
    all_sids = sorted(set(list(source_funnel.keys()) + list(source_stats.keys())))
    for sid in all_sids:
        sf = source_funnel.get(sid, {})
        raw_s = sf.get("raw", source_stats.get(sid, {}).get("raw", 0))
        stale_s = sf.get("stale", 0)
        dup_s = sf.get("duplicate", 0)
        elig_s = sf.get("eligible", 0)
        print(f"{sid:<18} | {raw_s:<6} | {stale_s:<6} | {dup_s:<10} | {elig_s:<8}")
    print("-" * 80)

    # Diagnostic title logging
    per_source = run_metadata.get("per_source_funnel", {})
    rejected_by_role = run_metadata.get("rejected_titles_by_role", {})
    for sid, counts in sorted(per_source.items()):
        if counts.get("location_pass", 0) > 0 and counts.get("role_pass", 0) == 0:
            sample_titles = rejected_by_role.get(sid, [])[:10]
            print(f"  [DIAGNOSTIC] {sid}: {counts.get('location_pass')} jobs passed location but 0 passed role.")
            print(f"               Rejected titles: {sample_titles}")

    print()
    print("-" * 80)
    print("REJECTIONS & DEDUPLICATION SUMMARY")
    print("-" * 80)
    print(f"Deduplicated (Skipped):        {dedup_count}")
    print(f"Rejected by Company (Infosys): {comp_rejected}")
    print(f"Rejected by Experience:        {exp_rejected}")
    print(f"Rejected by Location:          {loc_rejected}")
    print(f"Rejected by Role Relevance:    {role_rejected}")
    print()
    print("-" * 80)
    print("EMAIL DELIVERY & STATE PERSISTENCE")
    print("-" * 80)
    print(f"Jobs Emailed:                  {emailed_count}")
    print(f"Email Failures:                {email_failed_count}")
    print(f"Email Status:                  {run_metadata.get('email_status', 'N/A')}")
    print(f"State Persistence:             {state_persist}")
    print()
    print("LATENCY BREAKDOWN")
    print(f"Source retrieval:              {run_metadata.get('latency_retrieval_sec', 0)}s")
    print(f"Company filter:                {run_metadata.get('latency_company_sec', 0)}s")
    print(f"Experience filter:             {run_metadata.get('latency_experience_sec', 0)}s")
    print(f"Location filter:               {run_metadata.get('latency_location_sec', 0)}s")
    print(f"Role filter:                   {run_metadata.get('latency_role_sec', 0)}s")
    print(f"Matching:                      {run_metadata.get('latency_matching_sec', 0)}s")
    print(f"Email:                         {run_metadata.get('latency_email_sec', 0)}s")
    print(f"Total Latency:                 {run_metadata.get('latency_total_sec', 0)}s")
    print("=" * 80)


def run_worker_loop():
    """
    Near-Real-Time Persistent Worker Daemon.
    Continuously polls sources every ~15 minutes in a durable background loop.
    Survives unexpected errors and catches signals cleanly.
    """
    poll_interval_sec = int(os.environ.get("WORKER_POLL_INTERVAL_SECONDS", "900"))
    print("=" * 80)
    print("AI JOB HUNTER - PERSISTENT WORKER STARTED (PRODUCTION PERSISTENT DAEMON)")
    print(f"Polling loop interval: {poll_interval_sec}s (~{poll_interval_sec // 60}m) (Press Ctrl+C to stop).")
    print(f"Durable state directory: {STATE_DIR}")
    print("=" * 80)

    state = load_state()
    cycle = 1
    try:
        while True:
            cycle_start = datetime.now(timezone.utc)
            print()
            print("=" * 80)
            print(f"[WORKER] Starting polling cycle #{cycle} at {format_ist_and_utc(cycle_start)}...")
            print("=" * 80)
            try:
                matched_jobs, meta = run_pipeline_once(state, force_all=True)
                print(f"[WORKER] Cycle #{cycle} completed. Qualifying jobs delivered: {len(matched_jobs)}")
            except Exception as loop_err:
                print(f"ERROR: Worker cycle #{cycle} encountered an exception: {loop_err}")
                traceback.print_exc()

            cycle += 1
            print(f"[WORKER] Sleeping for {poll_interval_sec}s (~{poll_interval_sec // 60}m) until cycle #{cycle}...")
            time.sleep(poll_interval_sec)
    except KeyboardInterrupt:
        print("\n[WORKER] Worker loop terminated by user.")
    finally:
        save_state(state)


def main():
    """
    Standard one-shot execution used by GitHub Actions and scheduled runs.
    Performs exactly ONE complete job-hunting cycle and exits.
    """
    start_time = datetime.now(timezone.utc)
    print("=" * 80)
    print("AI JOB HUNTER - ONE-SHOT EXECUTION CYCLE")
    print(f"Start Time (UTC): {start_time.isoformat()}")
    print(f"Start Time (IST): {format_ist_and_utc(start_time)}")
    print("=" * 80)

    state = load_state()
    print(f"Persistent state loaded: {len(state.get('jobs', {}))} jobs tracked in database.")

    matched_jobs, run_metadata = run_pipeline_once(state, force_all=True)

    # Fail loudly if unhandled fatal errors occurred (e.g. state persistence failure or critical workflow failure)
    fatal_errors = run_metadata.get("workflow_errors", [])
    if fatal_errors:
        print()
        print(f"FATAL: One-shot execution cycle finished with {len(fatal_errors)} fatal error(s):")
        for err in fatal_errors:
            print(f"  - {err}")
        sys.exit(1)

    print()
    print(f"One-shot job-hunting cycle completed successfully ({len(matched_jobs)} qualifying jobs matched).")
    sys.exit(0)


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print("AI Job Hunter - Multi-Source Real-Time Pipeline")
        print("Usage: python main.py [options]")
        print("Options:")
        print("  --once             : Run exactly one complete job-hunting cycle and exit (default)")
        print("  --worker, --daemon : Run continuously as a lightweight background polling daemon")
        print("  --dry-run          : Run one acquisition pass without sending emails")
        print("  --help, -h         : Show this help message and exit")
        sys.exit(0)

    if "--worker" in sys.argv or "--daemon" in sys.argv or os.environ.get("RUN_MODE") == "worker":
        run_worker_loop()
    else:
        main()
