import os
import smtplib
from email.message import EmailMessage
from datetime import datetime, timezone


def send_email():
    username = os.environ["GMAIL_USERNAME"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]

    message = EmailMessage()

    message["From"] = username
    message["To"] = username
    message["Subject"] = "AI Job Hunter - Test Successful"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    message.set_content(
        f"""Your AI Job Hunter is working.

Test execution:
{now}

GitHub Actions successfully started the Python agent,
and the agent successfully sent this email.

Your PC can now be turned off during future runs.

Next step:
Connect the actual AI job-search logic.
"""
    )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(username, app_password)
        server.send_message(message)


def main():
    print("AI Job Hunter started.")
    send_email()
    print("Email sent successfully.")


if __name__ == "__main__":
    main()
