import mimetypes
import smtplib
from email.message import EmailMessage
from pathlib import Path

from config import (
    AUTO_SEND,
    CANDIDATE_NAME,
    CANDIDATE_ROLE,
    CANDIDATE_SKILLS,
    GMAIL_APP_PASSWORD,
    GMAIL_USERNAME,
    RELEVANT_PROJECTS,
    RESUME_PATH,
    VIDEO_RESUME_URL,
)


def _first_name(name):
    if not name:
        return "there"

    name = str(
        name
    ).strip()

    return name.split()[0]


def _select_project(recruiter):
    """
    Select a relevant project from the recruiter's
    technical/hiring context.

    This is deterministic and does not consume an
    LLM API call.
    """

    text_parts = [
        str(
            recruiter.get(
                "title",
                "",
            )
        ),
        str(
            recruiter.get(
                "headline",
                "",
            )
        ),
        str(
            recruiter.get(
                "company",
                "",
            )
        ),
        str(
            recruiter.get(
                "summary",
                "",
            )
        ),
    ]

    text = " ".join(
        text_parts
    ).lower()

    if (
        "rag" in text
        or "llm" in text
        or "generative ai" in text
    ):
        return (
            "Agentic Placement RAG"
        )

    if (
        "agent" in text
        or "multi-agent" in text
        or "ai agent" in text
    ):
        return (
            "Agentic Placement RAG"
        )

    if (
        "machine learning" in text
        or "ml" in text
        or "deep learning" in text
    ):
        return (
            "LLM Inference Optimization Lab"
        )

    return RELEVANT_PROJECTS[0]


def build_email(
    recruiter
):
    """
    Generate a concise personalized outreach email
    without requiring another LLM API call.
    """

    name = _first_name(
        recruiter.get(
            "name",
            "",
        )
    )

    company = (
        recruiter.get(
            "company"
        )
        or "your organization"
    )

    title = recruiter.get(
        "title",
        "",
    )

    project = _select_project(
        recruiter
    )

    score = recruiter.get(
        "recruiter_score",
        0,
    )

    subject = (
        f"AI/ML Engineer | "
        f"{CANDIDATE_NAME}"
    )

    lines = [
        f"Hi {name},",
        "",
        (
            f"I came across your work in "
            f"{title or 'technical recruiting'} "
            f"at {company} and wanted to reach out "
            f"directly."
        ),
        "",
        (
            f"I'm {CANDIDATE_NAME}, a 2026 CSE-AI/ML "
            f"graduate focused on production-oriented "
            f"Generative AI, LLMs, RAG and agentic AI."
        ),
        "",
        (
            f"One of my recent projects, "
            f"{project}, involved building practical "
            f"AI systems rather than only model demos."
        ),
        "",
        (
            "I'm currently looking for AI Engineer, "
            "Machine Learning Engineer, Generative AI, "
            "LLM or Agentic AI opportunities."
        ),
        "",
        (
            "If you're currently handling relevant "
            "openings, I'd be grateful if you could "
            "consider my profile."
        ),
        "",
        "I've attached my resume.",
    ]

    if VIDEO_RESUME_URL:
        lines.extend(
            [
                "",
                (
                    "60-second video introduction:"
                ),
                VIDEO_RESUME_URL,
            ]
        )

    lines.extend(
        [
            "",
            "Thank you for your time.",
            "",
            "Best,",
            CANDIDATE_NAME,
            CANDIDATE_ROLE,
        ]
    )

    body = "\n".join(
        lines
    )

    return subject, body


def _attach_resume(message):
    """
    Attach the candidate resume if it exists.
    """

    if not RESUME_PATH.exists():
        print(
            f"WARNING: Resume not found at "
            f"{RESUME_PATH}"
        )

        return

    mime_type, encoding = (
        mimetypes.guess_type(
            str(RESUME_PATH)
        )
    )

    if mime_type is None:
        mime_type = (
            "application/pdf"
        )

    maintype, subtype = (
        mime_type.split(
            "/",
            1,
        )
    )

    with RESUME_PATH.open(
        "rb"
    ) as file:
        data = file.read()

    message.add_attachment(
        data,
        maintype=maintype,
        subtype=subtype,
        filename=RESUME_PATH.name,
    )


def send_outreach_email(
    recruiter
):
    """
    Send one recruiter email.

    Returns:
        subject, status
    """

    email = (
        recruiter.get(
            "email"
        )
        or ""
    ).strip()

    if not email:
        raise ValueError(
            "Recruiter has no email address."
        )

    subject, body = build_email(
        recruiter
    )

    print()
    print(
        f"Preparing outreach to "
        f"{recruiter.get('name', 'Unknown')}"
    )

    print(
        f"Email: {email}"
    )

    print(
        f"Score: "
        f"{recruiter.get('recruiter_score', 0)}/100"
    )

    print(
        f"Subject: {subject}"
    )

    # --------------------------------------------------------
    # DRY RUN
    # --------------------------------------------------------

    if not AUTO_SEND:
        print(
            "AUTO_SEND=false"
        )

        print(
            "DRY RUN: email was NOT sent."
        )

        print()
        print("-" * 70)
        print(body)
        print("-" * 70)

        return (
            subject,
            "dry_run",
        )

    # --------------------------------------------------------
    # BUILD MESSAGE
    # --------------------------------------------------------

    message = EmailMessage()

    message["From"] = (
        GMAIL_USERNAME
    )

    message["To"] = email

    message["Subject"] = subject

    message.set_content(
        body
    )

    _attach_resume(
        message
    )

    # --------------------------------------------------------
    # SEND
    # --------------------------------------------------------

    print(
        "Sending recruiter email..."
    )

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
    ) as server:

        server.login(
            GMAIL_USERNAME,
            GMAIL_APP_PASSWORD,
        )

        server.send_message(
            message
        )

    print(
        "Recruiter email sent successfully."
    )

    return (
        subject,
        "sent",
    )
