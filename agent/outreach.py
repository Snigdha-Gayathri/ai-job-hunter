import mimetypes
import os
import smtplib
from email.message import EmailMessage

from config import (
    AUTO_SEND,
    GMAIL_APP_PASSWORD,
    GMAIL_USERNAME,
    get_active_profile,
    get_profile_projects,
    get_profile_resume_path,
    get_profile_roles,
    get_profile_skills,
    get_profile_video_resume_url,
)


def _first_name(name):
    if not name:
        return "there"

    name = str(name).strip()
    return name.split()[0]


def _format_natural_list(items: list[str], limit: int = 5) -> str:
    """Format a concise comma-separated list with 'and' before the last item."""
    cleaned = [str(item).strip() for item in items[:limit] if str(item).strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
    return f"{', '.join(cleaned[:-1])} and {cleaned[-1]}"


def _select_project(recruiter, profile: dict | None = None) -> tuple[str | None, str]:
    """
    Select a relevant project from the active profile based on the recruiter's
    technical/hiring context.

    Returns:
        (project_name, project_description)
    """
    active_profile = get_active_profile(profile)
    projects = get_profile_projects(active_profile)
    if not projects:
        return None, ""

    text_parts = [
        str(recruiter.get("title", "")),
        str(recruiter.get("headline", "")),
        str(recruiter.get("company", "")),
        str(recruiter.get("summary", "")),
    ]
    recruiter_text = " ".join(text_parts).lower()

    best_project = projects[0]
    best_overlap = 0

    for proj in projects:
        if isinstance(proj, dict):
            proj_text = " ".join(
                [
                    str(proj.get("name") or ""),
                    str(proj.get("description") or ""),
                    " ".join(str(t) for t in (proj.get("technologies") or [])),
                ]
            ).lower()
        else:
            proj_text = str(proj).lower()

        tokens = [tok for tok in proj_text.replace("-", " ").split() if len(tok) > 2]
        overlap = sum(1 for tok in tokens if tok in recruiter_text)
        if overlap > best_overlap:
            best_overlap = overlap
            best_project = proj

    if isinstance(best_project, dict):
        return (
            str(best_project.get("name") or "").strip() or None,
            str(best_project.get("description") or "").strip(),
        )
    return str(best_project).strip() or None, ""


def _build_intro_sentence(active_profile: dict) -> str:
    """Build candidate introduction sentence purely from the active profile."""
    candidate_name = str(active_profile.get("name") or "").strip()
    edu = active_profile.get("education") if isinstance(active_profile.get("education"), dict) else {}
    degree = str(edu.get("degree") or "").strip()
    field = str(edu.get("field") or "").strip()
    grad_year = str(edu.get("graduation_year") or "").strip()

    skills = get_profile_skills(active_profile)
    skills_str = _format_natural_list(skills, limit=4)

    work_exp = active_profile.get("work_experience") or []
    internships = active_profile.get("internships") or []

    edu_parts = [p for p in (grad_year, degree, f"in {field}" if field else "") if p]
    if edu_parts:
        identity = f"a {' '.join(edu_parts)} graduate"
    elif work_exp:
        first_exp = work_exp[0]
        if isinstance(first_exp, dict):
            role_title = str(first_exp.get("role") or first_exp.get("title") or "").strip()
            identity = f"a {role_title}" if role_title else "an engineering professional"
        else:
            identity = "an engineering professional"
    elif internships:
        identity = "an engineering candidate with hands-on internship experience"
    else:
        headline = str(active_profile.get("headline") or "").strip()
        identity = headline.split("|")[0].strip() if headline else "an engineering candidate"

    if skills_str:
        return f"I'm {candidate_name}, {identity} focused on {skills_str}."
    return f"I'm {candidate_name}, {identity}."


def build_email(recruiter, profile: dict | None = None):
    """
    Generate a concise personalized outreach email from the active profile
    without requiring an LLM API call.
    """
    active_profile = get_active_profile(profile)
    candidate_name = str(active_profile.get("name") or "").strip()
    target_roles = active_profile.get("target_roles") or get_profile_roles(active_profile)
    headline = str(active_profile.get("headline") or "").strip()
    primary_role = (
        headline.split("|")[0].strip()
        if headline
        else (str(target_roles[0]).strip() if target_roles else "Candidate")
    )

    name = _first_name(recruiter.get("name", ""))
    company = recruiter.get("company") or "your organization"
    title = recruiter.get("title", "")

    project_name, project_desc = _select_project(recruiter, profile=active_profile)
    resume_path = get_profile_resume_path(active_profile)
    video_resume_url = get_profile_video_resume_url(active_profile)

    subject = f"{primary_role} | {candidate_name}"

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
        _build_intro_sentence(active_profile),
    ]

    if project_name:
        if project_desc:
            lines.extend(["", f"One of my recent projects, {project_name}, focused on {project_desc.rstrip('.')}."])
        else:
            lines.extend(
                [
                    "",
                    f"One of my recent projects, {project_name}, involved building practical production-oriented systems.",
                ]
            )
    else:
        work_exp = active_profile.get("work_experience") or active_profile.get("internships") or []
        if work_exp:
            first = work_exp[0]
            if isinstance(first, dict):
                role = str(first.get("role") or first.get("title") or "").strip()
                org = str(first.get("company") or first.get("organization") or "").strip()
                if role and org:
                    lines.extend(["", f"Most recently, I worked as {role} at {org}."])
            elif isinstance(first, str) and first.strip():
                lines.extend(["", f"Recent experience: {first.strip()}."])

    roles_list_str = _format_natural_list([str(r) for r in target_roles], limit=5)
    if roles_list_str:
        lines.extend(
            [
                "",
                f"I'm currently looking for {roles_list_str} opportunities.",
            ]
        )

    lines.extend(
        [
            "",
            (
                "If you're currently handling relevant "
                "openings, I'd be grateful if you could "
                "consider my profile."
            ),
        ]
    )

    if resume_path is not None and resume_path.exists():
        lines.extend(["", "I've attached my resume."])

    if video_resume_url:
        lines.extend(
            [
                "",
                "60-second video introduction:",
                video_resume_url,
            ]
        )

    lines.extend(
        [
            "",
            "Thank you for your time.",
            "",
            "Best,",
            candidate_name,
        ]
    )
    if headline:
        lines.append(headline)

    body = "\n".join(lines)
    return subject, body


def _attach_resume(message, profile: dict | None = None):
    """
    Attach the active user profile's resume if configured and present on disk.
    """
    active_profile = get_active_profile(profile)
    resume_path = get_profile_resume_path(active_profile)

    if resume_path is None:
        print("INFO: No resume_path configured in profile; skipping attachment.")
        return

    if not resume_path.exists():
        print(f"WARNING: Resume not found at {resume_path}")
        return

    mime_type, encoding = mimetypes.guess_type(str(resume_path))
    if mime_type is None:
        mime_type = "application/pdf"

    maintype, subtype = mime_type.split("/", 1)

    with resume_path.open("rb") as file:
        data = file.read()

    message.add_attachment(
        data,
        maintype=maintype,
        subtype=subtype,
        filename=resume_path.name,
    )


def send_outreach_email(recruiter, profile: dict | None = None):
    """
    Send one recruiter email using the active user profile.

    Returns:
        subject, status
    """
    active_profile = get_active_profile(profile)
    email = (recruiter.get("email") or "").strip()

    if not email:
        raise ValueError("Recruiter has no email address.")

    resume_path = get_profile_resume_path(active_profile)
    auto_send = (
        os.getenv("AUTO_SEND", "").lower() == "true"
        if "AUTO_SEND" in os.environ
        else AUTO_SEND
    )
    username = os.getenv("GMAIL_USERNAME", "").strip() or GMAIL_USERNAME
    app_password = os.getenv("GMAIL_APP_PASSWORD", "").strip() or GMAIL_APP_PASSWORD

    if resume_path is not None and not resume_path.exists():
        if auto_send:
            raise FileNotFoundError(
                f"Configured resume file not found at '{resume_path}'. "
                "Update 'resume_path' in your profile or place the file at that path before sending outreach."
            )
        print(f"WARNING: Configured resume file not found at {resume_path}")

    subject, body = build_email(recruiter, profile=active_profile)

    print()
    print(f"Preparing outreach to {recruiter.get('name', 'Unknown')}")
    print(f"Email: {email}")
    print(f"Score: {recruiter.get('recruiter_score', 0)}/100")
    print(f"Subject: {subject}")

    # --------------------------------------------------------
    # DRY RUN
    # --------------------------------------------------------

    if not auto_send:
        print("AUTO_SEND=false")
        print("DRY RUN: email was NOT sent.")
        print()
        print("-" * 70)
        print(body)
        print("-" * 70)
        return (subject, "dry_run")

    # --------------------------------------------------------
    # BUILD MESSAGE
    # --------------------------------------------------------

    message = EmailMessage()
    message["From"] = username
    message["To"] = email
    message["Subject"] = subject
    message.set_content(body)

    _attach_resume(message, profile=active_profile)

    # --------------------------------------------------------
    # SEND
    # --------------------------------------------------------

    print("Sending recruiter email...")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(username, app_password)
        server.send_message(message)

    print("Recruiter email sent successfully.")
    return (subject, "sent")
