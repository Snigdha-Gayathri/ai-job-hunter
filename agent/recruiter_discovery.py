import requests

from config import (
    APIFY_TOKEN,
    MAX_RECRUITERS_DISCOVERED,
    RECRUITER_APIFY_URL,
    RECRUITER_JOB_TITLES,
    RECRUITER_LOCATIONS,
)


def search_recruiters():
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

    payload = {
        "profileScraperMode": (
            "Full + email search"
        ),

        "searchQuery": (
            '"AI recruiter" OR '
            '"technical recruiter" OR '
            '"talent acquisition" OR '
            '"technical talent acquisition" OR '
            '"AI hiring" OR '
            '"machine learning recruiter" OR '
            '"technology recruiter"'
        ),

        "maxItems": (
            MAX_RECRUITERS_DISCOVERED
        ),

        "locations": RECRUITER_LOCATIONS,

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

    try:
        response = requests.post(
            RECRUITER_APIFY_URL,
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
