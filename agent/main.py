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
    )

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
