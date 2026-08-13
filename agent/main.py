import os
import re
import smtplib
import requests

from email.message import EmailMessage
from datetime import datetime, timezone, timedelta

from job_matcher import score_job


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


def search_jobs():
    params = {
        "token": APIFY_TOKEN
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
        "scrapeJobDetails": True
    }

    print("Sending request to Apify...")

    response = requests.post(
        APIFY_URL,
        params=params,
        json=payload,
        timeout=180
    )

    print(f"Apify HTTP status: {response.status_code}")

    if response.status_code != 200:
        print("Apify response:")
        print(response.text[:3000])

    response.raise_for_status()

    return response.json()


def parse_posted_time(job):
    """
    Try to convert the Actor's posting timestamp into
    a timezone-aware datetime.
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

    # ISO 8601 timestamp
    try:
        normalized = raw.replace("Z", "+00:00")

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

    # Relative LinkedIn-style text
    text = raw.lower()

    now = datetime.now(timezone.utc)

    match = re.search(
        r"(\d+)\s*(minute|hour|day|week)",
        text
    )

    if match:
        value = int(match.group(1))
        unit = match.group(2)

        if unit == "minute":
            return now - timedelta(
                minutes=value
            )

        if unit == "hour":
            return now - timedelta(
                hours=value
            )

        if unit == "day":
            return now - timedelta(
                days=value
            )

        if unit == "week":
            return now - timedelta(
                weeks=value
            )

    if (
        "just now" in text
        or "today" in text
    ):
        return now

    return None


def filter_recent_jobs(jobs):
    """
    Keep only jobs posted within the last 72 hours.
    """

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(
            hours=MAX_JOB_AGE_HOURS
        )
    )

    recent = []
    unknown_timestamp = []

    for job in jobs:

        posted_time = parse_posted_time(
            job
        )

        if posted_time is None:
            unknown_timestamp.append(
                job
            )
            continue

        if posted_time >= cutoff:

            job["_parsed_posted_time"] = (
                posted_time.isoformat()
            )

            recent.append(job)

    print(
        f"Jobs from Apify: {len(jobs)}"
    )

    print(
        f"Jobs within 72 hours: "
        f"{len(recent)}"
    )

    print(
        f"Jobs with unknown posting time: "
        f"{len(unknown_timestamp)}"
    )

    return recent


def deduplicate_jobs(jobs):
    """
    Remove duplicate postings using
    the strongest available identifier.
    """

    seen = set()
    unique = []

    for job in jobs:

        job_id = (
            job.get("jobId")
            or job.get("id")
            or job.get("jobUrl")
            or job.get("url")
        )

        if not job_id:

            job_id = (
                f"{job.get('title', '').strip().lower()}|"
                f"{job.get('companyName', '').strip().lower()}"
            )

        if job_id in seen:
            continue

        seen.add(job_id)
        unique.append(job)

    print(
        f"Unique jobs: {len(unique)}"
    )

    return unique


def score_and_rank_jobs(jobs):
    """
    Score each job with the Groq AI matcher,
    keep strong matches, and rank them by score.
    """

    scored_jobs = []

    print()
    print("=" * 70)
    print("AI MATCHING STARTED")
    print("=" * 70)

    for index, job in enumerate(
        jobs,
        start=1
    ):

        print(
            f"[{index}/{len(jobs)}] "
            f"{job.get('title', 'Unknown title')} | "
            f"{job.get('companyName', 'Unknown company')}"
        )

        try:
            result = score_job(job)

            job["match_score"] = result.get(
                "match_score",
                0
            )

            job["qualification"] = result.get(
                "qualification",
                "UNKNOWN"
            )

            job["experience_fit"] = result.get(
                "experience_fit",
                "UNKNOWN"
            )

            job["technical_fit"] = result.get(
                "technical_fit",
                0
            )

            job["role_fit"] = result.get(
                "role_fit",
                0
            )

            job["key_matches"] = result.get(
                "key_matches",
                []
            )

            job["missing_requirements"] = result.get(
                "missing_requirements",
                []
            )

            job["concerns"] = result.get(
                "concerns",
                []
            )

            job["match_reason"] = result.get(
                "reason",
                ""
            )

            print(
                f"    Match score: "
                f"{job['match_score']}/100"
            )

            if job["match_score"] >= MIN_MATCH_SCORE:
                scored_jobs.append(job)

        except Exception as error:

            print(
                f"    Matcher failed: {error}"
            )

            continue

    scored_jobs.sort(
        key=lambda job: job.get(
            "match_score",
            0
        ),
        reverse=True
    )

    print()
    print(
        f"Jobs meeting "
        f"{MIN_MATCH_SCORE}+ threshold: "
        f"{len(scored_jobs)}"
    )

    print()
    print("TOP MATCHES")
    print("-" * 70)

    for index, job in enumerate(
        scored_jobs[:MAX_EMAIL_JOBS],
        start=1
    ):

        print(
            f"{index}. "
            f"{job.get('match_score', 0)}/100 | "
            f"{job.get('title', 'Unknown')} | "
            f"{job.get('companyName', 'Unknown')}"
        )

    return scored_jobs


def send_email(jobs):
    username = os.environ["GMAIL_USERNAME"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]

    message = EmailMessage()

    message["From"] = username
    message["To"] = username

    message["Subject"] = (
        f"AI Job Hunter - "
        f"{len(jobs[:MAX_EMAIL_JOBS])} High-Match Jobs"
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
        f"High-match jobs: "
        f"{len(jobs[:MAX_EMAIL_JOBS])}",
        "",
        "Freshness filter: "
        "Posted within the last 72 hours",
        f"AI match threshold: "
        f"{MIN_MATCH_SCORE}/100",
        "",
        "=" * 70,
        ""
    ]

    if not jobs:

        lines.extend([
            "No jobs met the AI match threshold.",
            "",
            "The agent will search again during "
            "the next run.",
            "",
            "=" * 70,
            ""
        ])

    for index, job in enumerate(
        jobs[:MAX_EMAIL_JOBS],
        start=1
    ):

        title = job.get(
            "title",
            "Unknown title"
        )

        company = job.get(
            "companyName",
            "Unknown company"
        )

        location = job.get(
            "location",
            "Unknown location"
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
            0
        )

        qualification = job.get(
            "qualification",
            "UNKNOWN"
        )

        experience_fit = job.get(
            "experience_fit",
            "UNKNOWN"
        )

        technical_fit = job.get(
            "technical_fit",
            0
        )

        role_fit = job.get(
            "role_fit",
            0
        )

        key_matches = job.get(
            "key_matches",
            []
        )

        missing_requirements = job.get(
            "missing_requirements",
            []
        )

        concerns = job.get(
            "concerns",
            []
        )

        reason = job.get(
            "match_reason",
            ""
        )

        lines.extend([
            f"{index}. {title}",
            "",
            f"AI MATCH SCORE: "
            f"{match_score}/100",
            f"Qualification: "
            f"{qualification}",
            f"Experience fit: "
            f"{experience_fit}",
            f"Technical fit: "
            f"{technical_fit}/100",
            f"Role fit: "
            f"{role_fit}/100",
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
            ""
        ])

        if key_matches:

            lines.append(
                "KEY MATCHES:"
            )

            for item in key_matches:
                lines.append(
                    f"  + {item}"
                )

            lines.append("")

        if missing_requirements:

            lines.append(
                "MISSING REQUIREMENTS:"
            )

            for item in missing_requirements:
                lines.append(
                    f"  - {item}"
                )

            lines.append("")

        if concerns:

            lines.append(
                "CONCERNS:"
            )

            for item in concerns:
                lines.append(
                    f"  ! {item}"
                )

            lines.append("")

        lines.extend([
            "-" * 70,
            ""
        ])

    message.set_content(
        "\n".join(lines)
    )

    print(
        "Connecting to Gmail..."
    )

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465
    ) as server:

        server.login(
            username,
            app_password
        )

        server.send_message(
            message
        )


def main():

    print("=" * 70)
    print("AI JOB HUNTER STARTED")
    print("=" * 70)

    jobs = search_jobs()

    recent_jobs = filter_recent_jobs(
        jobs
    )

    unique_jobs = deduplicate_jobs(
        recent_jobs
    )

    matched_jobs = score_and_rank_jobs(
        unique_jobs
    )import json
import os
import re
import smtplib

import requests

from email.message import EmailMessage
from datetime import datetime, timezone, timedelta
from pathlib import Path

from job_matcher import (
    locally_filter_jobs,
    score_jobs_batch,
)


APIFY_TOKEN = os.environ["APIFY_API_TOKEN"]

ACTOR_ID = "automation-lab~linkedin-jobs-scraper"

APIFY_URL = (
    f"https://api.apify.com/v2/acts/"
    f"{ACTOR_ID}/run-sync-get-dataset-items"
)


# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

MAX_EMAIL_JOBS = 20
MAX_SCRAPED_JOBS = 50

# Jobs newer than this are considered fresh.
MAX_JOB_AGE_HOURS = 72

# Final AI/local threshold.
MIN_MATCH_SCORE = 75

# Persistent state.
STATE_DIR = Path("state")
STATE_FILE = STATE_DIR / "seen_jobs.json"

# Keep the state bounded.
MAX_SEEN_JOBS = 5000


# ---------------------------------------------------------
# STATE
# ---------------------------------------------------------

def load_state() -> dict:
    """
    Load persistent job state.

    GitHub Actions restores this directory from the
    Actions cache before main.py starts.
    """

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    if not STATE_FILE.exists():
        return {
            "jobs": {},
        }

    try:
        with STATE_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:
            state = json.load(file)

        if not isinstance(state, dict):
            raise ValueError("Invalid state format.")

        if "jobs" not in state:
            state["jobs"] = {}

        return state

    except Exception as error:
        print(
            f"Could not load state: {error}"
        )

        return {
            "jobs": {},
        }


def save_state(state: dict) -> None:
    """
    Persist job state.
    """

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Keep only the newest MAX_SEEN_JOBS records.
    jobs = state.get("jobs", {})

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


def get_job_id(job: dict) -> str:
    """
    Generate the strongest available stable identifier.
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

    return f"{title}|{company}|{location}"


def remove_previously_seen_jobs(
    jobs: list[dict],
    state: dict,
) -> list[dict]:
    """
    Remove jobs already processed by previous runs.

    ZERO API calls.
    """

    seen = state.setdefault(
        "jobs",
        {},
    )

    new_jobs = []

    now = datetime.now(
        timezone.utc
    ).isoformat()

    for job in jobs:
        job_id = get_job_id(job)

        job["_job_id"] = job_id

        if job_id in seen:
            seen[job_id]["last_seen"] = now
            continue

        seen[job_id] = {
            "first_seen": now,
            "last_seen": now,
            "title": job.get("title", ""),
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
        f"{len(jobs) - len(new_jobs)}"
    )

    return new_jobs


# ---------------------------------------------------------
# APIFY
# ---------------------------------------------------------

def search_jobs() -> list[dict]:
    """
    Perform exactly ONE Apify request.
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

        # Keep this enabled because the job description is
        # needed for local filtering and AI ranking.
        "scrapeJobDetails": True,
    }

    print(
        "Sending ONE request to Apify..."
    )

    response = requests.post(
        APIFY_URL,
        params=params,
        json=payload,
        timeout=180,
    )

    print(
        f"Apify HTTP status: "
        f"{response.status_code}"
    )

    if response.status_code == 429:
        print(
            "Apify rate limit reached."
        )
        print(
            "The run will stop without retrying."
        )
        return []

    response.raise_for_status()

    data = response.json()

    if not isinstance(data, list):
        raise ValueError(
            "Apify returned an unexpected response."
        )

    print(
        f"Apify returned {len(data)} jobs."
    )

    return data


# ---------------------------------------------------------
# DATE HANDLING
# ---------------------------------------------------------

def parse_posted_time(
    job: dict,
):
    """
    Convert common LinkedIn-style timestamps into UTC.
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

    # ISO 8601.
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

    text = raw.lower()

    now = datetime.now(
        timezone.utc
    )

    match = re.search(
        r"(\d+)\s*(minute|hour|day|week)",
        text,
    )

    if match:
        value = int(
            match.group(1)
        )

        unit = match.group(2)

        if unit == "minute":
            return now - timedelta(
                minutes=value
            )

        if unit == "hour":
            return now - timedelta(
                hours=value
            )

        if unit == "day":
            return now - timedelta(
                days=value
            )

        if unit == "week":
            return now - timedelta(
                weeks=value
            )

    if (
        "just now" in text
        or "today" in text
    ):
        return now

    return None


def filter_recent_jobs(
    jobs: list[dict],
) -> list[dict]:
    """
    Keep recent jobs.

    Jobs with unknown timestamps are retained only when
    they are otherwise usable. This prevents the previous
    behavior of silently throwing potentially valid jobs away.
    """

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(
            hours=MAX_JOB_AGE_HOURS
        )
    )

    recent = []
    unknown_timestamp = []

    for job in jobs:
        posted_time = parse_posted_time(
            job
        )

        if posted_time is None:
            unknown_timestamp.append(
                job
            )
            continue

        if posted_time >= cutoff:
            job["_parsed_posted_time"] = (
                posted_time.isoformat()
            )

            recent.append(job)

    print(
        f"Jobs from Apify: {len(jobs)}"
    )

    print(
        f"Jobs within {MAX_JOB_AGE_HOURS} hours: "
        f"{len(recent)}"
    )

    print(
        f"Jobs with unknown posting time: "
        f"{len(unknown_timestamp)}"
    )

    # Keep unknown timestamp jobs available, but put them
    # after known-recent jobs.
    recent.extend(
        unknown_timestamp
    )

    return recent


# ---------------------------------------------------------
# DEDUPLICATION
# ---------------------------------------------------------

def deduplicate_jobs(
    jobs: list[dict],
) -> list[dict]:
    """
    Remove duplicates inside the current Apify response.
    """

    seen = set()
    unique = []

    for job in jobs:
        job_id = get_job_id(job)

        if job_id in seen:
            continue

        seen.add(job_id)

        job["_job_id"] = job_id

        unique.append(job)

    print(
        f"Unique jobs in current run: "
        f"{len(unique)}"
    )

    return unique


# ---------------------------------------------------------
# MATCHING
# ---------------------------------------------------------

def score_and_rank_jobs(
    jobs: list[dict],
) -> list[dict]:
    """
    Local filter -> one Groq batch request -> ranking.
    """

    if not jobs:
        print(
            "No jobs available for matching."
        )
        return []

    print()
    print("=" * 70)
    print("LOCAL JOB FILTER")
    print("=" * 70)

    candidates = locally_filter_jobs(
        jobs
    )

    if not candidates:
        print(
            "No plausible AI/ML jobs survived "
            "the local filter."
        )
        return []

    print()
    print("=" * 70)
    print("AI BATCH MATCHING")
    print("=" * 70)

    scored_jobs = score_jobs_batch(
        candidates
    )

    scored_jobs.sort(
        key=lambda job: job.get(
            "match_score",
            0,
        ),
        reverse=True,
    )

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
        f"Jobs meeting {MIN_MATCH_SCORE}+ threshold: "
        f"{len(matched_jobs)}"
    )

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
            f"{job.get('title', 'Unknown')} | "
            f"{job.get('companyName', 'Unknown')}"
        )

    return matched_jobs


# ---------------------------------------------------------
# EMAIL
# ---------------------------------------------------------

def send_email(
    jobs: list[dict],
    total_new_jobs: int,
) -> None:
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
        f"{len(jobs[:MAX_EMAIL_JOBS])} High-Match Jobs"
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
        f"High-match jobs: "
        f"{len(jobs[:MAX_EMAIL_JOBS])}",
        "",
        "Architecture:",
        "- 1 Apify request",
        "- Local relevance filtering",
        "- Maximum 1 Groq batch request",
        "- Persistent seen-job cache",
        "",
        "Freshness filter:",
        f"Posted within the last "
        f"{MAX_JOB_AGE_HOURS} hours",
        f"AI match threshold: "
        f"{MIN_MATCH_SCORE}/100",
        "",
        "=" * 70,
        "",
    ]

    if not jobs:
        lines.extend(
            [
                "No new jobs met the AI match threshold.",
                "",
                "This does NOT necessarily mean there are "
                "no relevant jobs.",
                "",
                "Possible reasons:",
                "- No new jobs were discovered.",
                "- Jobs were already seen in a previous run.",
                "- Jobs failed the local AI/ML filter.",
                "- No jobs reached the match threshold.",
                "",
                "The system will continue searching "
                "during the next run.",
                "",
                "=" * 70,
                "",
            ]
        )

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
                f"AI MATCH SCORE: "
                f"{match_score}/100",
                f"Qualification: "
                f"{qualification}",
                f"Experience fit: "
                f"{experience_fit}",
                f"Technical fit: "
                f"{technical_fit}/100",
                f"Role fit: "
                f"{role_fit}/100",
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

        if key_matches:
            lines.append(
                "KEY MATCHES:"
            )

            for item in key_matches:
                lines.append(
                    f"  + {item}"
                )

            lines.append("")

        if missing_requirements:
            lines.append(
                "MISSING REQUIREMENTS:"
            )

            for item in missing_requirements:
                lines.append(
                    f"  - {item}"
                )

            lines.append("")

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

    message.set_content(
        "\n".join(lines)
    )

    print(
        "Connecting to Gmail..."
    )

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


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main():
    print("=" * 70)
    print("AI JOB HUNTER STARTED")
    print("=" * 70)

    state = load_state()

    # -----------------------------------------------------
    # 1. ONE APIFY CALL
    # -----------------------------------------------------

    jobs = search_jobs()

    if not jobs:
        print(
            "No jobs returned from Apify."
        )

        # Still send a diagnostic email rather than
        # silently claiming that zero jobs exist.
        send_email(
            jobs=[],
            total_new_jobs=0,
        )

        save_state(state)

        return

    # -----------------------------------------------------
    # 2. LOCAL FRESHNESS FILTER
    # -----------------------------------------------------

    recent_jobs = filter_recent_jobs(
        jobs
    )

    # -----------------------------------------------------
    # 3. LOCAL DEDUPLICATION
    # -----------------------------------------------------

    unique_jobs = deduplicate_jobs(
        recent_jobs
    )

    # -----------------------------------------------------
    # 4. PERSISTENT DEDUPLICATION
    # -----------------------------------------------------

    new_jobs = remove_previously_seen_jobs(
        unique_jobs,
        state,
    )

    save_state(state)

    # -----------------------------------------------------
    # 5. LOCAL AI/ML FILTER
    # -----------------------------------------------------

    matched_jobs = score_and_rank_jobs(
        new_jobs
    )

    # -----------------------------------------------------
    # 6. EMAIL
    # -----------------------------------------------------

    print()
    print(
        f"Sending "
        f"{len(matched_jobs[:MAX_EMAIL_JOBS])} "
        f"high-match jobs..."
    )

    send_email(
        matched_jobs,
        total_new_jobs=len(new_jobs),
    )

    print(
        "Email sent successfully."
    )

    print("=" * 70)
    print(
        "AI JOB HUNTER COMPLETED"
    )
    print("=" * 70)


if __name__ == "__main__":
    main()

    print()
    print(
        f"Sending "
        f"{len(matched_jobs[:MAX_EMAIL_JOBS])} "
        f"high-match jobs..."
    )

    send_email(
        matched_jobs
    )

    print(
        "Email sent successfully."
    )

    print("=" * 70)


if __name__ == "__main__":
    main()
