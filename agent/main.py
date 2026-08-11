import os
import re
import smtplib
import requests

from email.message import EmailMessage
from datetime import datetime, timezone, timedelta


APIFY_TOKEN = os.environ["APIFY_API_TOKEN"]

ACTOR_ID = "automation-lab~linkedin-jobs-scraper"

APIFY_URL = (
    f"https://api.apify.com/v2/acts/"
    f"{ACTOR_ID}/run-sync-get-dataset-items"
)

MAX_EMAIL_JOBS = 20
MAX_SCRAPED_JOBS = 50
MAX_JOB_AGE_HOURS = 72


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
        dt = datetime.fromisoformat(normalized)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except ValueError:
        pass

    # Relative LinkedIn-style text
    text = raw.lower()

    now = datetime.now(timezone.utc)

    match = re.search(r"(\d+)\s*(minute|hour|day|week)", text)

    if match:
        value = int(match.group(1))
        unit = match.group(2)

        if unit == "minute":
            return now - timedelta(minutes=value)

        if unit == "hour":
            return now - timedelta(hours=value)

        if unit == "day":
            return now - timedelta(days=value)

        if unit == "week":
            return now - timedelta(weeks=value)

    if "just now" in text or "today" in text:
        return now

    return None


def filter_recent_jobs(jobs):
    """
    Keep only jobs posted within the last 72 hours.
    """

    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=MAX_JOB_AGE_HOURS
    )

    recent = []
    unknown_timestamp = []

    for job in jobs:

        posted_time = parse_posted_time(job)

        if posted_time is None:
            unknown_timestamp.append(job)
            continue

        if posted_time >= cutoff:
            job["_parsed_posted_time"] = posted_time.isoformat()
            recent.append(job)

    print(f"Jobs from Apify: {len(jobs)}")
    print(f"Jobs within 72 hours: {len(recent)}")
    print(f"Jobs with unknown posting time: {len(unknown_timestamp)}")

    return recent


def deduplicate_jobs(jobs):
    """
    Remove duplicate postings using the strongest available identifier.
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
            # Fall back to title + company
            job_id = (
                f"{job.get('title', '').strip().lower()}|"
                f"{job.get('companyName', '').strip().lower()}"
            )

        if job_id in seen:
            continue

        seen.add(job_id)
        unique.append(job)

    print(f"Unique jobs: {len(unique)}")

    return unique


def send_email(jobs):
    username = os.environ["GMAIL_USERNAME"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]

    message = EmailMessage()

    message["From"] = username
    message["To"] = username

    message["Subject"] = (
        f"AI Job Hunter - {len(jobs)} Fresh Jobs"
    )

    now = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    lines = [
        "AI JOB HUNTER REPORT",
        "",
        f"Run time: {now}",
        f"Fresh jobs found: {len(jobs)}",
        "",
        "Filter: Posted within the last 72 hours",
        "",
        "=" * 70,
        ""
    ]

    if not jobs:
        lines.extend([
            "No new jobs matching the current search were found.",
            "",
            "The agent will search again during the next run."
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

        lines.extend([
            f"{index}. {title}",
            "",
            f"Company: {company}",
            f"Location: {location}",
            f"Posted: {posted}",
            f"Apply: {apply_url}",
            f"LinkedIn: {linkedin_url}",
            "",
            "-" * 70,
            ""
        ])

    message.set_content("\n".join(lines))

    print("Connecting to Gmail...")

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465
    ) as server:

        server.login(
            username,
            app_password
        )

        server.send_message(message)


def main():

    print("=" * 70)
    print("AI JOB HUNTER STARTED")
    print("=" * 70)

    jobs = search_jobs()

    recent_jobs = filter_recent_jobs(jobs)

    unique_jobs = deduplicate_jobs(recent_jobs)

    print()
    print("Fresh jobs:")
    print("-" * 70)

    for job in unique_jobs[:10]:

        print(
            f"{job.get('title')} | "
            f"{job.get('companyName')} | "
            f"{job.get('location')} | "
            f"{job.get('postedAt')}"
        )

    print()
    print("Sending email...")

    send_email(unique_jobs)

    print("Email sent successfully.")

    print("=" * 70)


if __name__ == "__main__":
    main()
