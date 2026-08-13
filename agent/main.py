import json
import os
import re
import smtplib
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

APIFY_TOKEN = os.environ["APIFY_API_TOKEN"]

ACTOR_ID = "automation-lab~linkedin-jobs-scraper"

APIFY_URL = (
    f"https://api.apify.com/v2/acts/"
    f"{ACTOR_ID}/run-sync-get-dataset-items"
)

MAX_EMAIL_JOBS = 20
MAX_SCRAPED_JOBS = 50
MAX_JOB_AGE_HOURS = 72
MIN_MATCH_SCORE = 75

# Persistent state directory.
STATE_DIR = Path("state")
STATE_FILE = STATE_DIR / "seen_jobs.json"

# Prevent the state file from growing forever.
MAX_SEEN_JOBS = 5000


# ============================================================
# STATE MANAGEMENT
# ============================================================

def load_state():
    """
    Load persistent job state from disk.

    The GitHub Actions workflow restores this directory
    from the Actions cache before running main.py.
    """

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    if not STATE_FILE.exists():
        return {
            "jobs": {}
        }

    try:
        with STATE_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:
            state = json.load(file)

        if not isinstance(state, dict):
            raise ValueError(
                "State file does not contain a JSON object."
            )

        if "jobs" not in state:
            state["jobs"] = {}

        if not isinstance(state["jobs"], dict):
            state["jobs"] = {}

        return state

    except Exception as error:
        print(
            f"WARNING: Could not load state file: {error}"
        )

        return {
            "jobs": {}
        }


def save_state(state):
    """
    Save persistent job state to disk.
    """

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    jobs = state.get(
        "jobs",
        {},
    )

    # Keep only the newest MAX_SEEN_JOBS records.
    if len(jobs) > MAX_SEEN_JOBS:
        sorted_items = sorted(
            jobs.items(),
            key=lambda item: item[1].get(
                "last_seen",
                "",
            ),
            reverse=True,
        )

        state["jobs"] = dict(
            sorted_items[:MAX_SEEN_JOBS]
        )

    with STATE_FILE.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            state,
            file,
            indent=2,
        )

    print(
        f"State saved: {len(state['jobs'])} jobs tracked."
    )


def get_job_id(job):
    """
    Generate a stable identifier for a job.

    Preference order:
    1. jobId
    2. id
    3. jobUrl
    4. url
    5. title + company + location
    """

    job_id = (
        job.get("jobId")
        or job.get("id")
        or job.get("jobUrl")
        or job.get("url")
    )

    if job_id:
        return str(job_id)

    title = str(
        job.get("title") or ""
    ).strip().lower()

    company = str(
        job.get("companyName") or ""
    ).strip().lower()

    location = str(
        job.get("location") or ""
    ).strip().lower()

    return (
        f"{title}|{company}|{location}"
    )


def remove_previously_seen_jobs(
    jobs,
    state,
):
    """
    Remove jobs that have already been processed
    by previous workflow runs.

    This performs ZERO external API calls.
    """

    seen_jobs = state.setdefault(
        "jobs",
        {},
    )

    new_jobs = []

    now = datetime.now(
        timezone.utc
    ).isoformat()

    skipped_count = 0

    for job in jobs:
        job_id = get_job_id(job)

        job["_job_id"] = job_id

        if job_id in seen_jobs:
            seen_jobs[job_id]["last_seen"] = now
            skipped_count += 1
            continue

        seen_jobs[job_id] = {
            "first_seen": now,
            "last_seen": now,
            "title": job.get(
                "title",
                "",
            ),
            "company": job.get(
                "companyName",
                "",
            ),
        }

        new_jobs.append(job)

    print(
        f"New jobs: {len(new_jobs)}"
    )

    print(
        f"Previously seen jobs skipped: "
        f"{skipped_count}"
    )

    return new_jobs


# ============================================================
# APIFY JOB SEARCH
# ============================================================

def search_jobs():
    """
    Make exactly ONE Apify API request.

    Apify may return HTTP 200 or HTTP 201 when the
    synchronous actor execution succeeds.
    """

    params = {
        "token": APIFY_TOKEN,
    }

    payload = {
        "searchQuery": (
            "AI Engineer Machine Learning Engineer "
            "ML Engineer Generative AI LLM RAG"
        ),
        "location": "India",
        "maxJobs": MAX_SCRAPED_JOBS,
        "jobType": "F",
        "experienceLevel": "2",
        "datePosted": "r604800",
        "sortBy": "DD",
        "scrapeJobDetails": True,
    }

    print()
    print("=" * 70)
    print("APIFY JOB SEARCH")
    print("=" * 70)

    print("Sending ONE request to Apify...")

    try:
        response = requests.post(
            APIFY_URL,
            params=params,
            json=payload,
            timeout=180,
        )

    except requests.RequestException as error:
        print(
            f"Apify request failed: {error}"
        )
        return []

    print(
        f"Apify HTTP status: "
        f"{response.status_code}"
    )

    # --------------------------------------------------------
    # IMPORTANT:
    # Apify can return 200 OR 201 for a successful
    # run-sync-get-dataset-items request.
    # --------------------------------------------------------

    if response.status_code not in (200, 201):
        if response.status_code == 429:
            print(
                "Apify rate limit reached."
            )

            retry_after = response.headers.get(
                "Retry-After"
            )

            if retry_after:
                print(
                    f"Apify requested retry after "
                    f"{retry_after} seconds."
                )

            print(
                "No retry will be performed during this run."
            )

            return []

        print(
            "Apify returned an actual HTTP error:"
        )

        print(
            response.text[:3000]
        )

        return []

    # --------------------------------------------------------
    # SUCCESS
    # --------------------------------------------------------

    try:
        data = response.json()

    except ValueError:
        print(
            "Apify returned invalid JSON."
        )

        print(
            response.text[:3000]
        )

        return []

    # The successful response is a list of scraped jobs.
    if isinstance(data, list):
        print(
            f"Apify returned {len(data)} jobs."
        )

        return data

    # Defensive handling in case Apify returns an object.
    if isinstance(data, dict):
        print(
            "Apify returned a JSON object instead of "
            "a job list."
        )

        # Some Apify responses may wrap dataset items.
        for key in (
            "items",
            "data",
            "results",
        ):
            if isinstance(
                data.get(key),
                list,
            ):
                jobs = data[key]

                print(
                    f"Apify returned "
                    f"{len(jobs)} jobs."
                )

                return jobs

    print(
        "Unexpected Apify response format."
    )

    print(
        str(data)[:3000]
    )

    return []


# ============================================================
# POSTING DATE PARSING
# ============================================================

def parse_posted_time(job):
    """
    Convert the job's posting timestamp into
    a timezone-aware UTC datetime.

    Supports:
    - ISO 8601
    - relative LinkedIn-style timestamps
    - 'today'
    - 'just now'
    """

    raw = (
        job.get("postedAt")
        or job.get("postedDate")
        or job.get("postedAtText")
        or ""
    )

    if not raw:
        return None

    raw = str(raw).strip()

    # --------------------------------------------------------
    # ISO 8601
    # --------------------------------------------------------

    try:
        normalized = raw.replace(
            "Z",
            "+00:00",
        )

        dt = datetime.fromisoformat(
            normalized
        )

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return dt.astimezone(
            timezone.utc
        )

    except ValueError:
        pass

    # --------------------------------------------------------
    # Relative timestamps
    # --------------------------------------------------------

    text = raw.lower()

    now = datetime.now(
        timezone.utc
    )

    match = re.search(
        r"(\d+)\s*(minute|minutes|hour|hours|day|days|week|weeks)",
        text,
    )

    if match:
        value = int(
            match.group(1)
        )

        unit = match.group(2)

        if unit.startswith("minute"):
            return now - timedelta(
                minutes=value
            )

        if unit.startswith("hour"):
            return now - timedelta(
                hours=value
            )

        if unit.startswith("day"):
            return now - timedelta(
                days=value
            )

        if unit.startswith("week"):
            return now - timedelta(
                weeks=value
            )

    # --------------------------------------------------------
    # Immediate timestamps
    # --------------------------------------------------------

    if (
        "just now" in text
        or "today" in text
        or "just posted" in text
    ):
        return now

    return None


def filter_recent_jobs(jobs):
    """
    Keep jobs posted within MAX_JOB_AGE_HOURS.

    Jobs whose timestamp cannot be parsed are retained
    and allowed to pass to the local relevance filter.

    This avoids accidentally discarding valid jobs because
    the scraper returned an unfamiliar timestamp format.
    """

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(
            hours=MAX_JOB_AGE_HOURS
        )
    )

    recent_jobs = []
    unknown_timestamp_jobs = []

    for job in jobs:
        posted_time = parse_posted_time(
            job
        )

        if posted_time is None:
            unknown_timestamp_jobs.append(
                job
            )
            continue

        if posted_time >= cutoff:
            job["_parsed_posted_time"] = (
                posted_time.isoformat()
            )

            recent_jobs.append(
                job
            )

    print()
    print(
        f"Jobs from Apify: {len(jobs)}"
    )

    print(
        f"Jobs within "
        f"{MAX_JOB_AGE_HOURS} hours: "
        f"{len(recent_jobs)}"
    )

    print(
        f"Jobs with unknown posting time: "
        f"{len(unknown_timestamp_jobs)}"
    )

    # Unknown timestamps are retained after known-recent
    # jobs. Local filtering will decide whether they are
    # actually relevant.
    recent_jobs.extend(
        unknown_timestamp_jobs
    )

    return recent_jobs


# ============================================================
# CURRENT-RUN DEDUPLICATION
# ============================================================

def deduplicate_jobs(jobs):
    """
    Remove duplicate jobs within the current Apify response.
    """

    seen = set()
    unique_jobs = []

    for job in jobs:
        job_id = get_job_id(job)

        if job_id in seen:
            continue

        seen.add(job_id)

        job["_job_id"] = job_id

        unique_jobs.append(job)

    print(
        f"Unique jobs in current run: "
        f"{len(unique_jobs)}"
    )

    return unique_jobs


# ============================================================
# LOCAL + AI MATCHING
# ============================================================

def score_and_rank_jobs(jobs):
    """
    Matching pipeline:

    1. Local deterministic filtering
    2. Maximum 15 jobs sent to Groq
    3. ONE Groq batch request
    4. Local fallback if Groq fails
    5. Sort by match score
    6. Keep jobs >= MIN_MATCH_SCORE
    """

    if not jobs:
        print(
            "No jobs available for matching."
        )

        return []

    # --------------------------------------------------------
    # LOCAL FILTER
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("LOCAL JOB FILTER")
    print("=" * 70)

    candidates = locally_filter_jobs(
        jobs
    )

    if not candidates:
        print(
            "No jobs survived the local AI/ML filter."
        )

        return []

    print(
        f"Local filter produced "
        f"{len(candidates)} candidates."
    )

    # --------------------------------------------------------
    # SINGLE GROQ BATCH REQUEST
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("AI BATCH MATCHING")
    print("=" * 70)

    print(
        "Important: production path performs "
        "ONE Groq request."
    )

    scored_jobs = score_jobs_batch(
        candidates
    )

    if not scored_jobs:
        print(
            "No scored jobs returned."
        )

        return []

    # --------------------------------------------------------
    # SORT
    # --------------------------------------------------------

    scored_jobs.sort(
        key=lambda job: job.get(
            "match_score",
            0,
        ),
        reverse=True,
    )

    # --------------------------------------------------------
    # THRESHOLD
    # --------------------------------------------------------

    matched_jobs = [
        job
        for job in scored_jobs
        if job.get(
            "match_score",
            0,
        ) >= MIN_MATCH_SCORE
    ]

    print()
    print(
        f"Jobs meeting "
        f"{MIN_MATCH_SCORE}+ threshold: "
        f"{len(matched_jobs)}"
    )

    # --------------------------------------------------------
    # DISPLAY TOP RESULTS
    # --------------------------------------------------------

    print()
    print("TOP MATCHES")
    print("-" * 70)

    for index, job in enumerate(
        scored_jobs[:MAX_EMAIL_JOBS],
        start=1,
    ):
        print(
            f"{index}. "
            f"{job.get('match_score', 0)}/100 | "
            f"{job.get('title', 'Unknown title')} | "
            f"{job.get('companyName', 'Unknown company')}"
        )

    return matched_jobs


# ============================================================
# EMAIL
# ============================================================

def send_email(
    jobs,
    total_new_jobs,
):
    """
    Send the final job report through Gmail SMTP.
    """

    username = os.environ[
        "GMAIL_USERNAME"
    ]

    app_password = os.environ[
        "GMAIL_APP_PASSWORD"
    ]

    message = EmailMessage()

    message["From"] = username
    message["To"] = username

    message["Subject"] = (
        f"AI Job Hunter - "
        f"{len(jobs[:MAX_EMAIL_JOBS])} "
        f"High-Match Jobs"
    )

    now = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    lines = [
        "AI JOB HUNTER - AI MATCHED REPORT",
        "",
        f"Run time: {now}",
        f"New jobs discovered: {total_new_jobs}",
        (
            "High-match jobs: "
            f"{len(jobs[:MAX_EMAIL_JOBS])}"
        ),
        "",
        "ARCHITECTURE:",
        "- One Apify batch request",
        "- Local relevance filtering",
        "- Maximum one Groq batch request",
        "- Persistent seen-job cache",
        "- Local fallback when Groq is unavailable",
        "",
        (
            "Freshness filter: "
            f"Last {MAX_JOB_AGE_HOURS} hours"
        ),
        (
            "AI match threshold: "
            f"{MIN_MATCH_SCORE}/100"
        ),
        "",
        "=" * 70,
        "",
    ]

    # --------------------------------------------------------
    # NO MATCHES
    # --------------------------------------------------------

    if not jobs:
        lines.extend(
            [
                "No new jobs met the AI match threshold.",
                "",
                "This does NOT necessarily mean that "
                "zero jobs were found.",
                "",
                "Possible reasons:",
                "- No new jobs were discovered.",
                "- Jobs were already seen in an earlier run.",
                "- Jobs failed the local AI/ML filter.",
                "- Jobs did not meet the match threshold.",
                "",
                (
                    "The agent will search again during "
                    "the next scheduled run."
                ),
                "",
                "=" * 70,
                "",
            ]
        )

    # --------------------------------------------------------
    # JOBS
    # --------------------------------------------------------

    for index, job in enumerate(
        jobs[:MAX_EMAIL_JOBS],
        start=1,
    ):
        title = job.get(
            "title",
            "Unknown title",
        )

        company = job.get(
            "companyName",
            "Unknown company",
        )

        location = job.get(
            "location",
            "Unknown location",
        )

        posted = (
            job.get("postedAt")
            or job.get("postedDate")
            or job.get("postedAtText")
            or "Unknown"
        )

        apply_url = (
            job.get("applyUrl")
            or job.get("jobUrl")
            or job.get("url")
            or "No application URL"
        )

        linkedin_url = (
            job.get("jobUrl")
            or job.get("url")
            or "No LinkedIn URL"
        )

        match_score = job.get(
            "match_score",
            0,
        )

        qualification = job.get(
            "qualification",
            "UNKNOWN",
        )

        experience_fit = job.get(
            "experience_fit",
            "UNKNOWN",
        )

        technical_fit = job.get(
            "technical_fit",
            0,
        )

        role_fit = job.get(
            "role_fit",
            0,
        )

        key_matches = job.get(
            "key_matches",
            [],
        )

        missing_requirements = job.get(
            "missing_requirements",
            [],
        )

        concerns = job.get(
            "concerns",
            [],
        )

        reason = job.get(
            "match_reason",
            "",
        )

        lines.extend(
            [
                f"{index}. {title}",
                "",
                (
                    f"AI MATCH SCORE: "
                    f"{match_score}/100"
                ),
                (
                    f"Qualification: "
                    f"{qualification}"
                ),
                (
                    f"Experience fit: "
                    f"{experience_fit}"
                ),
                (
                    f"Technical fit: "
                    f"{technical_fit}/100"
                ),
                (
                    f"Role fit: "
                    f"{role_fit}/100"
                ),
                "",
                f"Company: {company}",
                f"Location: {location}",
                f"Posted: {posted}",
                "",
                f"Apply: {apply_url}",
                f"LinkedIn: {linkedin_url}",
                "",
                "WHY IT MATCHES:",
                reason,
                "",
            ]
        )

        # ----------------------------------------------------
        # KEY MATCHES
        # ----------------------------------------------------

        if key_matches:
            lines.append(
                "KEY MATCHES:"
            )

            for item in key_matches:
                lines.append(
                    f"  + {item}"
                )

            lines.append("")

        # ----------------------------------------------------
        # MISSING REQUIREMENTS
        # ----------------------------------------------------

        if missing_requirements:
            lines.append(
                "MISSING REQUIREMENTS:"
            )

            for item in missing_requirements:
                lines.append(
                    f"  - {item}"
                )

            lines.append("")

        # ----------------------------------------------------
        # CONCERNS
        # ----------------------------------------------------

        if concerns:
            lines.append(
                "CONCERNS:"
            )

            for item in concerns:
                lines.append(
                    f"  ! {item}"
                )

            lines.append("")

        lines.extend(
            [
                "-" * 70,
                "",
            ]
        )

    # --------------------------------------------------------
    # SEND
    # --------------------------------------------------------

    message.set_content(
        "\n".join(lines)
    )

    print()
    print(
        "Connecting to Gmail..."
    )

    try:
        with smtplib.SMTP_SSL(
            "smtp.gmail.com",
            465,
        ) as server:

            server.login(
                username,
                app_password,
            )

            server.send_message(
                message
            )

    except Exception as error:
        print(
            f"Gmail send failed: {error}"
        )

        raise

    print(
        "Email sent successfully."
    )


# ============================================================
# MAIN PIPELINE
# ============================================================

def main():
    """
    Main AI Job Hunter pipeline.

    Architecture:

        Apify
          |
          | ONE API CALL
          v
        Raw jobs
          |
          v
        Freshness filter
          |
          v
        Current-run deduplication
          |
          v
        Persistent seen-job filtering
          |
          v
        Local AI/ML filtering
          |
          v
        Maximum 15 candidates
          |
          | ONE GROQ API CALL
          v
        AI batch scoring
          |
          v
        Match threshold
          |
          v
        Gmail report
    """

    print("=" * 70)
    print("AI JOB HUNTER STARTED")
    print("=" * 70)

    print()
    print(
        "API strategy:"
    )
    print(
        "  Apify: 1 request"
    )
    print(
        "  Groq: maximum 1 request"
    )
    print(
        "  Local processing: unlimited"
    )

    # ========================================================
    # 1. LOAD STATE
    # ========================================================

    state = load_state()

    print(
        f"Previously tracked jobs: "
        f"{len(state.get('jobs', {}))}"
    )

    # ========================================================
    # 2. SEARCH APIFY
    # ========================================================

    jobs = search_jobs()

    if not jobs:
        print()
        print(
            "No jobs were returned by Apify."
        )

        # Send a diagnostic email instead of pretending
        # the job search successfully found zero jobs.
        send_email(
            jobs=[],
            total_new_jobs=0,
        )

        save_state(state)

        print(
            "AI JOB HUNTER FINISHED"
        )

        return

    # ========================================================
    # 3. FRESHNESS FILTER
    # ========================================================

    recent_jobs = filter_recent_jobs(
        jobs
    )

    # ========================================================
    # 4. CURRENT-RUN DEDUPLICATION
    # ========================================================

    unique_jobs = deduplicate_jobs(
        recent_jobs
    )

    # ========================================================
    # 5. PERSISTENT DEDUPLICATION
    # ========================================================

    new_jobs = remove_previously_seen_jobs(
        unique_jobs,
        state,
    )

    # Save immediately so discovered jobs are recorded
    # even if later processing fails.
    save_state(state)

    # ========================================================
    # 6. MATCH
    # ========================================================

    matched_jobs = score_and_rank_jobs(
        new_jobs
    )

    # ========================================================
    # 7. EMAIL
    # ========================================================

    print()
    print("=" * 70)
    print("EMAIL REPORT")
    print("=" * 70)

    print(
        f"New jobs: {len(new_jobs)}"
    )

    print(
        f"High-match jobs: "
        f"{len(matched_jobs[:MAX_EMAIL_JOBS])}"
    )

    send_email(
        jobs=matched_jobs,
        total_new_jobs=len(new_jobs),
    )

    # ========================================================
    # 8. SAVE STATE AGAIN
    # ========================================================

    save_state(state)

    print()
    print("=" * 70)
    print("AI JOB HUNTER COMPLETED")
    print("=" * 70)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
