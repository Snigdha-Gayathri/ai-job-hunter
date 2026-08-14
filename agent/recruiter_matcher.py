import re

from config import (
    CANDIDATE_SKILLS,
    MIN_RECRUITER_SCORE,
    TARGET_ROLES,
)


RECRUITER_KEYWORDS = {
    "recruiter",
    "recruiting",
    "talent acquisition",
    "talent partner",
    "talent sourcer",
    "technical recruiter",
    "technical sourcer",
    "technical talent",
    "hiring",
}

AI_KEYWORDS = {
    "artificial intelligence",
    "ai",
    "machine learning",
    "ml",
    "generative ai",
    "genai",
    "llm",
    "large language model",
    "rag",
    "agentic",
    "deep learning",
    "data science",
}


def _combined_text(recruiter):
    """
    Build searchable text from the entire recruiter
    record.

    This lets us benefit from:
    - headline
    - title
    - summary
    - current company
    - experience
    - skills
    """

    parts = []

    for key in (
        "name",
        "title",
        "headline",
        "summary",
        "about",
        "company",
        "companyName",
        "location",
    ):
        value = recruiter.get(key)

        if value:
            parts.append(
                str(value)
            )

    experience = (
        recruiter.get(
            "experience"
        )
        or recruiter.get(
            "experiences"
        )
        or recruiter.get(
            "workExperience"
        )
        or []
    )

    if isinstance(
        experience,
        list,
    ):
        for item in experience:
            if isinstance(
                item,
                dict,
            ):
                parts.extend(
                    str(value)
                    for value in item.values()
                    if value
                )
            else:
                parts.append(
                    str(item)
                )

    skills = (
        recruiter.get(
            "skills"
        )
        or []
    )

    if isinstance(
        skills,
        list,
    ):
        parts.extend(
            str(item)
            for item in skills
        )

    return " ".join(
        parts
    ).lower()


def _contains_any(
    text,
    keywords,
):
    return [
        keyword
        for keyword in keywords
        if keyword.lower() in text
    ]


def extract_email(recruiter):
    """
    Handle several likely email field names
    returned by enrichment actors.
    """

    possible_keys = [
        "email",
        "emailAddress",
        "workEmail",
        "businessEmail",
        "corporateEmail",
        "contactEmail",
    ]

    for key in possible_keys:
        value = recruiter.get(key)

        if isinstance(
            value,
            str,
        ):
            value = value.strip()

            if (
                "@" in value
                and "." in value.split(
                    "@"
                )[-1]
            ):
                return value.lower()

    # Some actors return emails as arrays.
    for key in (
        "emails",
        "emailAddresses",
    ):
        value = recruiter.get(key)

        if isinstance(
            value,
            list,
        ):
            for item in value:
                if isinstance(
                    item,
                    str,
                ) and "@" in item:
                    return item.lower()

                if isinstance(
                    item,
                    dict,
                ):
                    candidate = (
                        item.get(
                            "email"
                        )
                    )

                    if (
                        isinstance(
                            candidate,
                            str,
                        )
                        and "@" in candidate
                    ):
                        return candidate.lower()

    return ""


def normalize_recruiter(
    recruiter
):
    """
    Convert different Apify field names into
    one internal representation.
    """

    name = (
        recruiter.get("name")
        or recruiter.get("fullName")
        or "Unknown Recruiter"
    )

    title = (
        recruiter.get("title")
        or recruiter.get(
            "headline"
        )
        or recruiter.get(
            "currentJobTitle"
        )
        or ""
    )

    company = (
        recruiter.get(
            "companyName"
        )
        or recruiter.get(
            "company"
        )
        or ""
    )

    linkedin_url = (
        recruiter.get(
            "linkedinUrl"
        )
        or recruiter.get(
            "linkedin_url"
        )
        or recruiter.get(
            "url"
        )
        or recruiter.get(
            "profileUrl"
        )
        or ""
    )

    location = (
        recruiter.get(
            "location"
        )
        or ""
    )

    normalized = {
        **recruiter,

        "name": str(
            name
        ).strip(),

        "title": str(
            title
        ).strip(),

        "company": str(
            company
        ).strip(),

        "linkedin_url": str(
            linkedin_url
        ).strip(),

        "location": str(
            location
        ).strip(),

        "email": extract_email(
            recruiter
        ),
    }

    return normalized


def score_recruiter(
    recruiter
):
    """
    Deterministic recruiter relevance score.

    Maximum = 100.

    30 points: recruiter role
    30 points: AI/ML relevance
    20 points: matching target roles
    10 points: active LinkedIn presence
    10 points: usable work email
    """

    recruiter = normalize_recruiter(
        recruiter
    )

    text = _combined_text(
        recruiter
    )

    score = 0

    reasons = []

    # --------------------------------------------------------
    # RECRUITING ROLE
    # --------------------------------------------------------

    recruiter_matches = _contains_any(
        text,
        RECRUITER_KEYWORDS,
    )

    if recruiter_matches:
        score += 30

        reasons.append(
            "Works in recruiting or talent acquisition."
        )

    # --------------------------------------------------------
    # AI/ML RELEVANCE
    # --------------------------------------------------------

    ai_matches = _contains_any(
        text,
        AI_KEYWORDS,
    )

    if ai_matches:
        score += min(
            30,
            10 + (
                len(ai_matches) * 5
            ),
        )

        reasons.append(
            "Profile shows AI/ML hiring relevance: "
            + ", ".join(
                ai_matches[:5]
            )
        )

    # --------------------------------------------------------
    # TARGET ROLE MATCH
    # --------------------------------------------------------

    target_matches = []

    for role in TARGET_ROLES:
        if role.lower() in text:
            target_matches.append(
                role
            )

    if target_matches:
        score += 20

        reasons.append(
            "Profile references target roles: "
            + ", ".join(
                target_matches[:5]
            )
        )

    # --------------------------------------------------------
    # CANDIDATE SKILL MATCH
    # --------------------------------------------------------

    skill_matches = []

    for skill in CANDIDATE_SKILLS:
        if skill.lower() in text:
            skill_matches.append(
                skill
            )

    if skill_matches:
        score += min(
            10,
            len(skill_matches) * 2,
        )

        reasons.append(
            "Recruiter profile contains relevant technical areas."
        )

    # --------------------------------------------------------
    # RECENT ACTIVITY
    # --------------------------------------------------------

    if (
        recruiter.get(
            "recentlyPostedOnLinkedIn"
        )
        is True
    ):
        score += 10

        reasons.append(
            "Recently active on LinkedIn."
        )

    # Some actors return a generic recent activity
    # field rather than the boolean.
    if recruiter.get(
        "recentPosts"
    ):
        score = min(
            100,
            score + 5,
        )

    # --------------------------------------------------------
    # EMAIL
    # --------------------------------------------------------

    if recruiter.get(
        "email"
    ):
        score += 10

        reasons.append(
            "A potential work email was found."
        )

    score = min(
        score,
        100,
    )

    recruiter[
        "recruiter_score"
    ] = score

    recruiter[
        "score_reasons"
    ] = reasons

    recruiter[
        "ai_keyword_matches"
    ] = ai_matches

    recruiter[
        "target_role_matches"
    ] = target_matches

    recruiter[
        "skill_matches"
    ] = skill_matches

    return recruiter


def filter_and_rank_recruiters(
    recruiters
):
    """
    Normalize, score and rank recruiters.
    """

    scored = []

    for recruiter in recruiters:
        try:
            normalized = normalize_recruiter(
                recruiter
            )

            scored_recruiter = score_recruiter(
                normalized
            )

            score = scored_recruiter.get(
                "recruiter_score",
                0,
            )

            if score >= MIN_RECRUITER_SCORE:
                scored.append(
                    scored_recruiter
                )

        except Exception as error:
            print(
                f"Recruiter scoring failed: "
                f"{error}"
            )

    scored.sort(
        key=lambda recruiter: recruiter.get(
            "recruiter_score",
            0,
        ),
        reverse=True,
    )

    return scored
