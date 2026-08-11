import os
import smtplib
import requests

from email.message import EmailMessage
from datetime import datetime, timezone


APIFY_TOKEN = os.environ["APIFY_API_TOKEN"]

ACTOR_ID = "automation-lab~linkedin-jobs-scraper"

APIFY_URL = (
    f"https://api.apify.com/v2/acts/"
    f"{ACTOR_ID}/run-sync-get-dataset-items"
)


def search_jobs():
    params = {
        "token": APIFY_TOKEN
    }

    payload = {
        "searchQuery": "AI Engineer",
        "location": "India",
        "maxJobs": 50,
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

    response.raise_for_status()

    return response.json()


def send_email(jobs):
    username = os.environ["GMAIL_USERNAME"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]

    message = EmailMessage()

    message["From"] = username
    message["To"] = username
    message["Subject"] = (
        f"AI Job Hunter - {len(jobs)} Jobs Found"
    )

    now = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    lines = [
        "AI JOB HUNTER REPORT",
        "",
        f"Run time: {now}",
        f"Jobs found: {len(jobs)}",
        "",
        "=" * 60,
        ""
    ]

    for index, job in enumerate(jobs[:20], start=1):

        title = job.get("title", "Unknown title")
        company = job.get("companyName", "Unknown company")
        location = job.get("location", "Unknown location")
        posted = job.get("postedAt", "Unknown")

        url = (
            job.get("applyUrl")
            or job.get("jobUrl")
            or job.get("url")
            or "No application URL"
        )

        lines.extend([
            f"{index}. {title}",
            f"Company: {company}",
            f"Location: {location}",
            f"Posted: {posted}",
            f"Apply: {url}",
            "",
            "-" * 60,
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

    print("=" * 60)
    print("AI JOB HUNTER STARTED")
    print("=" * 60)

    jobs = search_jobs()

    print(f"Jobs returned: {len(jobs)}")
    print()

    for job in jobs[:10]:

        print(
            f"{job.get('title')} | "
            f"{job.get('companyName')} | "
            f"{job.get('location')} | "
            f"{job.get('postedAt')}"
        )

    print()
    print("Sending email...")

    send_email(jobs)

    print("Email sent successfully.")
    print("=" * 60)


if __name__ == "__main__":
    main()
