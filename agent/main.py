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
        "searchQueries": [
            "AI Engineer",
            "Machine Learning Engineer",
            "ML Engineer",
            "Generative AI",
            "LLM Engineer",
            "RAG Engineer",
            "AI/ML Engineer"
        ],
        "location": "India",
        "maxJobs": 50,
        "jobType": "F",
        "experienceLevel": "2",
        "datePosted": "r259200",
        "sortBy": "DD",
        "scrapeJobDetails": True
    }

    response = requests.post(
        APIFY_URL,
        params=params,
        json=payload,
        timeout=180
    )

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

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(username, app_password)
        server.send_message(message)


def main():
    print("Starting Apify job search...")

    jobs = search_jobs()

    print(f"Jobs returned: {len(jobs)}")

    for job in jobs[:10]:
        print(
            f"{job.get('title')} | "
            f"{job.get('companyName')} | "
            f"{job.get('location')} | "
            f"{job.get('postedAt')}"
        )

    print("Sending email...")

    send_email(jobs)

    print("Email sent successfully.")


if __name__ == "__main__":
    main()
