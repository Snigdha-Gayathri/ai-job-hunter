import requests

from config import (
    APIFY_TOKEN,
    MAX_RECRUITERS_DISCOVERED,
    RECRUITER_APIFY_URL,
    RECRUITER_JOB_TITLES,
    RECRUITER_LOCATIONS,
    get_active_profile,
    get_profile_locations,
    get_profile_remote_prefs,
    get_profile_roles,
    profile_targets_ai_roles,
)


def _build_recruiter_search_query(profile: dict) -> str:
    """Build recruiter search query dynamically from the active profile."""
    if profile_targets_ai_roles(profile):
        return (
            '"AI recruiter" OR '
            '"technical recruiter" OR '
            '"talent acquisition" OR '
            '"technical talent acquisition" OR '
            '"AI hiring" OR '
            '"machine learning recruiter" OR '
            '"technology recruiter"'
        )
    roles = (profile.get("target_roles") or get_profile_roles(profile))[:3]
    role_clauses = " OR ".join(f'"{r} hiring"' for r in roles if r)
    base = '"technical recruiter" OR "talent acquisition" OR "technical talent acquisition" OR "technology recruiter"'
    return f"{base} OR {role_clauses}" if role_clauses else base


def _build_recruiter_locations(profile: dict) -> list[str]:
    """Derive recruiter search locations from the active profile."""
    remote_prefs = get_profile_remote_prefs(profile)
    locs: list[str] = []
    home = str(remote_prefs.get("home_country") or "").strip()
    if home:
        locs.append(home)
    for loc in get_profile_locations(profile):
        if loc not in locs:
            locs.append(loc)
    if remote_prefs.get("accept_remote") and "Remote" not in locs:
        locs.append("Remote")
    return locs or RECRUITER_LOCATIONS


def search_recruiters(profile: dict | None = None):
    """
    Perform exactly ONE Apify request.

    The actor performs:
        LinkedIn profile discovery
        +
        profile enrichment
        +
        email search

    We deliberately do not make a second request
    for individual email enrichment.
    """
    active_profile = get_active_profile(profile)

    payload = {
        "profileScraperMode": (
            "Full + email search"
        ),

        "searchQuery": _build_recruiter_search_query(active_profile),

        "maxItems": (
            MAX_RECRUITERS_DISCOVERED
        ),

        "locations": _build_recruiter_locations(active_profile),

        "currentJobTitles": (
            RECRUITER_JOB_TITLES
        ),

        "recentlyPostedOnLinkedIn": True,

        "takePages": 1,

        "startPage": 1,

        "autoQuerySegmentation": False,
    }

    print()
    print("=" * 70)
    print("RECRUITER DISCOVERY")
    print("=" * 70)

    print(
        "Sending ONE recruiter discovery request to Apify..."
    )

    headers = {
        "Content-Type": "application/json",
    }
    if APIFY_TOKEN:
        headers["Authorization"] = f"Bearer {APIFY_TOKEN}"

    try:
        response = requests.post(
            RECRUITER_APIFY_URL,
            headers=headers,
            params={
                "token": APIFY_TOKEN,
            },
            json=payload,
            timeout=240,
        )

    except requests.RequestException as error:
        print(
            f"Recruiter Apify request failed: "
            f"{error}"
        )

        return []

    print(
        f"Apify HTTP status: "
        f"{response.status_code}"
    )

    # Apify synchronous endpoints may return
    # 200 or 201 for successful execution.
    if response.status_code not in (
        200,
        201,
    ):
        if response.status_code == 429:
            print(
                "Apify rate limit reached."
            )

            print(
                "No retry will be performed."
            )

            return []

        print(
            "Apify returned an actual error:"
        )

        print(
            response.text[:3000]
        )

        return []

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

    if isinstance(data, list):
        print(
            f"Apify returned "
            f"{len(data)} recruiter profiles."
        )

        return data

    if isinstance(data, dict):
        for key in (
            "items",
            "data",
            "results",
        ):
            value = data.get(key)

            if isinstance(value, list):
                print(
                    f"Apify returned "
                    f"{len(value)} recruiter profiles."
                )

                return value

    print(
        "Unexpected recruiter response format:"
    )

    print(
        str(data)[:3000]
    )

    return []
