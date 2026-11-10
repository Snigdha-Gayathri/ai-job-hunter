import hashlib
import json
import os
from pathlib import Path


# ============================================================
# PROJECT PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

CONFIG_DIR = BASE_DIR / "config"
STATE_DIR = BASE_DIR / "state"
ASSETS_DIR = BASE_DIR / "assets"

PROFILE_EXAMPLE_PATH = CONFIG_DIR / "profile.example.json"
PROFILE_PRIVATE_PATH = CONFIG_DIR / "profile.private.json"

RECRUITER_STATE_FILE = (
    STATE_DIR / "recruiters.json"
)

OUTREACH_STATE_FILE = (
    STATE_DIR / "outreach_history.json"
)


# ============================================================
# APIFY
# ============================================================

def get_clean_apify_token(token: str | None = None) -> str:
    """
    Safely retrieves and sanitizes the Apify API token.
    Supports APIFY_API_TOKEN, APIFY_TOKEN, and APIFY_API_KEY environment variables.
    Strips leading/trailing whitespace, newlines, surrounding quotes, and 'Bearer ' prefix.
    Never prints, logs, or exposes the token value.
    """
    if token is not None:
        raw = token
    else:
        raw = (
            os.getenv("APIFY_API_KEY")
            or os.getenv("APIFY_API_TOKEN")
            or os.getenv("APIFY_TOKEN")
            or ""
        )
    cleaned = raw.strip().strip("'\"")
    if cleaned.startswith("Bearer "):
        cleaned = cleaned[7:].strip()
    return cleaned


def verify_apify_auth(token: str | None = None) -> tuple[bool, str, dict]:
    """
    Safely verifies the Apify API token against GET https://api.apify.com/v2/users/me.
    Never logs or prints the token value.

    Returns:
        (is_valid: bool, status: str, details: dict)
    """
    import requests

    resolved = get_clean_apify_token(token)
    if not resolved:
        print("APIFY AUTH: INVALID (No Apify API token configured)")
        return False, "MISSING_TOKEN", {"error": "Token not configured in environment"}

    url = "https://api.apify.com/v2/users/me"
    headers = {
        "Authorization": f"Bearer {resolved}",
        "Content-Type": "application/json",
    }
    params = {"token": resolved}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code == 200:
            try:
                data = resp.json().get("data", {})
                username = data.get("username") or data.get("id") or "verified_account"
            except Exception:
                username = "verified_account"
            print(f"APIFY AUTH: VALID (Account: {username})")
            return True, "VALID", {"status_code": 200, "username": username}

        elif resp.status_code == 401:
            print("APIFY AUTH: INVALID (HTTP 401: User was not found or authentication token is not valid)")
            return False, "INVALID_TOKEN", {"status_code": 401, "error": "user-or-token-not-found"}

        elif resp.status_code == 403:
            print("APIFY AUTH: VALID (HTTP 403: Token is authentic, but account has usage limitations / platform restrictions)")
            return True, "USAGE_LIMITED", {"status_code": 403, "error": "usage-limited"}

        else:
            print(f"APIFY AUTH: INVALID (HTTP {resp.status_code})")
            return False, f"HTTP_{resp.status_code}", {"status_code": resp.status_code}

    except requests.RequestException as exc:
        print(f"APIFY AUTH: INVALID (Network connection error: {type(exc).__name__})")
        return False, "NETWORK_ERROR", {"error": str(exc)}


APIFY_TOKEN = get_clean_apify_token()

# Current Apify recruiter discovery actor.
#
# This actor supports:
# - LinkedIn profile search
# - job-title filtering
# - location filtering
# - Full + email search
#
# Source:
# harvestapi/linkedin-profile-search

RECRUITER_ACTOR_ID = (
    "harvestapi~linkedin-profile-search"
)

RECRUITER_APIFY_URL = (
    "https://api.apify.com/v2/actors/"
    f"{RECRUITER_ACTOR_ID}"
    "/run-sync-get-dataset-items"
)


# ============================================================
# RECRUITER SEARCH
# ============================================================

MAX_RECRUITERS_DISCOVERED = 25

MAX_RECRUITERS_TO_CONTACT = 2

MIN_RECRUITER_SCORE = 75

RECRUITER_LOCATIONS = [
    "India",
    "United States",
    "Remote",
]


RECRUITER_JOB_TITLES = [
    "Technical Recruiter",
    "Technical Talent Acquisition",
    "Talent Acquisition Specialist",
    "Talent Acquisition Partner",
    "Talent Acquisition Recruiter",
    "IT Recruiter",
    "Technology Recruiter",
    "Technical Sourcer",
    "Talent Sourcer",
    "Recruiter",
    "Recruiting Specialist",
    "Talent Partner",
    "Hiring Manager",
]


# ============================================================
# OUTREACH
# ============================================================

# Safety switch.
#
# Set to "true" in GitHub Actions only when you want
# automatic sending enabled.

AUTO_SEND = (
    os.getenv(
        "AUTO_SEND",
        "false",
    ).lower()
    == "true"
)

MAX_EMAILS_PER_RUN = 2

MIN_EMAIL_SCORE = 85


# ============================================================
# EMAIL
# ============================================================

GMAIL_USERNAME = os.getenv(
    "GMAIL_USERNAME",
    "",
)

GMAIL_APP_PASSWORD = os.getenv(
    "GMAIL_APP_PASSWORD",
    "",
)


# ============================================================
# USER PROFILE LOADING & VALIDATION
# ============================================================

class ProfileConfigurationError(ValueError):
    """Raised when the active user profile is missing or invalid."""


_ACTIVE_PROFILE: dict | None = None
_ACTIVE_PROFILE_KEY: tuple | None = None


def _dedupe_non_empty(items: list) -> list[str]:
    """Return deduplicated, stripped non-empty strings preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items or []:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key not in seen:
            seen.add(key)
            result.append(text)
    return result


def get_profile_skills(profile: dict) -> list[str]:
    """Extract a deduplicated flat list of skills from a profile dictionary."""
    raw_skills = profile.get("skills")
    if isinstance(raw_skills, list):
        return _dedupe_non_empty(raw_skills)
    if isinstance(raw_skills, dict):
        collected: list = []
        core = raw_skills.get("core")
        if isinstance(core, list):
            collected.extend(core)
        categories = raw_skills.get("categories")
        if isinstance(categories, dict):
            for vals in categories.values():
                if isinstance(vals, list):
                    collected.extend(vals)
        return _dedupe_non_empty(collected)
    return []


def get_profile_roles(profile: dict) -> list[str]:
    """Extract deduplicated target roles and role families from a profile."""
    combined: list = []
    for key in ("target_roles", "target_role_families"):
        vals = profile.get(key)
        if isinstance(vals, list):
            combined.extend(vals)
    return _dedupe_non_empty(combined)


def get_profile_locations(profile: dict) -> list[str]:
    """Extract preferred target locations from a profile."""
    raw = profile.get("target_locations")
    if isinstance(raw, list):
        return _dedupe_non_empty(raw)
    return []


def get_profile_remote_prefs(profile: dict) -> dict:
    """Extract normalized remote-work preferences from a profile."""
    raw = profile.get("remote_preferences")
    prefs = raw if isinstance(raw, dict) else {}
    locations_lower = {loc.lower() for loc in get_profile_locations(profile)}
    accept_remote = bool(prefs.get("accept_remote", False)) or ("remote" in locations_lower)

    allowed_raw = prefs.get("allowed_remote_regions")
    allowed_regions = _dedupe_non_empty(allowed_raw) if isinstance(allowed_raw, list) else []

    home_country = str(prefs.get("home_country") or "").strip()
    if home_country and home_country.lower() not in {r.lower() for r in allowed_regions}:
        allowed_regions.insert(0, home_country)

    return {
        "accept_remote": accept_remote,
        "accept_hybrid_in_target_cities": bool(
            prefs.get("accept_hybrid_in_target_cities", True)
        ),
        "home_country": home_country,
        "allowed_remote_regions": allowed_regions,
    }


def get_profile_excluded_companies(profile: dict) -> list[str]:
    """Extract excluded company names from a profile."""
    raw = profile.get("excluded_companies")
    if isinstance(raw, list):
        return _dedupe_non_empty(raw)
    return []


def get_profile_resume_path(profile: dict) -> Path | None:
    """Resolve the active resume path from env override or profile configuration."""
    raw_path = os.getenv("RESUME_PATH", "").strip() or str(profile.get("resume_path") or "").strip()
    if not raw_path:
        return None
    p = Path(raw_path)
    if not p.is_absolute():
        return BASE_DIR / p
    return p


def get_profile_video_resume_url(profile: dict) -> str:
    """Resolve optional video resume URL from env override or profile configuration."""
    return (
        os.getenv("VIDEO_RESUME_URL", "").strip()
        or str(profile.get("video_resume_url") or "").strip()
    )


def get_profile_projects(profile: dict) -> list[dict | str]:
    """Extract configured projects from a profile."""
    raw = profile.get("projects")
    if not isinstance(raw, list):
        return []
    projects: list[dict | str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            projects.append(item.strip())
        elif isinstance(item, dict) and str(item.get("name") or "").strip():
            projects.append(item)
    return projects


def get_profile_experience_prefs(profile: dict) -> dict:
    """Extract normalized experience preferences from a profile."""
    raw = profile.get("experience_preferences")
    prefs = raw if isinstance(raw, dict) else {}
    level = str(prefs.get("level") or "").strip().lower()
    max_yoe = prefs.get("max_required_yoe")
    if max_yoe is not None:
        try:
            max_yoe = int(max_yoe)
        except (TypeError, ValueError):
            max_yoe = None

    yoe = prefs.get("years_of_experience")
    if yoe is not None:
        try:
            yoe = float(yoe)
        except (TypeError, ValueError):
            yoe = None

    if "exclude_senior_titles" in prefs and prefs["exclude_senior_titles"] is not None:
        exclude_senior = bool(prefs["exclude_senior_titles"])
    else:
        exclude_senior = level in ("", "entry_level", "entry-level", "fresher", "junior", "intern", "graduate") and (
            max_yoe is None or max_yoe <= 2
        )

    if "accept_fresher" in prefs and prefs["accept_fresher"] is not None:
        accept_fresher = bool(prefs["accept_fresher"])
    else:
        accept_fresher = level in ("", "entry_level", "entry-level", "fresher", "junior", "intern", "graduate")

    keywords_raw = prefs.get("experience_keywords")
    keywords = _dedupe_non_empty(keywords_raw) if isinstance(keywords_raw, list) else []

    return {
        "level": level or ("entry_level" if accept_fresher else ""),
        "years_of_experience": yoe,
        "max_required_yoe": max_yoe,
        "accept_fresher": accept_fresher,
        "exclude_senior_titles": exclude_senior,
        "experience_keywords": keywords,
    }


def profile_targets_ai_roles(profile: dict) -> bool:
    """Return True if the active profile targets AI/ML/GenAI/LLM roles."""
    ai_markers = (
        "ai",
        "ml",
        "machine learning",
        "deep learning",
        "generative ai",
        "genai",
        "llm",
        "agentic",
        "nlp",
        "computer vision",
    )
    roles_text = " ".join(get_profile_roles(profile)).lower()
    return any(marker in roles_text for marker in ai_markers)


def get_profile_path(profile_path: str | Path | None = None) -> Path:
    """Resolve the filesystem path to the profile JSON file."""
    if profile_path is not None:
        return Path(profile_path)
    env_path = os.getenv("PROFILE_PATH", "").strip()
    if env_path:
        p = Path(env_path)
        return p if p.is_absolute() else (BASE_DIR / p)
    return PROFILE_PRIVATE_PATH


def validate_profile(profile: dict) -> dict:
    """Validate that a loaded profile contains required non-empty configuration."""
    if not isinstance(profile, dict):
        raise ProfileConfigurationError(
            "Invalid profile format: expected a JSON object. "
            "Copy config/profile.example.json to config/profile.private.json and fill in your details."
        )

    missing: list[str] = []
    if not str(profile.get("name") or "").strip():
        missing.append("name")
    if not get_profile_skills(profile):
        missing.append("skills")
    if not get_profile_roles(profile):
        missing.append("target_roles / target_role_families")

    locations = get_profile_locations(profile)
    remote_prefs = get_profile_remote_prefs(profile)
    if not locations and not remote_prefs.get("accept_remote"):
        missing.append("target_locations (or remote_preferences.accept_remote=true)")

    if profile.get("excluded_companies") is not None and not isinstance(profile.get("excluded_companies"), list):
        missing.append("excluded_companies (must be a list)")
    if profile.get("resume_path") is not None and not isinstance(profile.get("resume_path"), str):
        missing.append("resume_path (must be a string)")
    if profile.get("video_resume_url") is not None and not isinstance(profile.get("video_resume_url"), str):
        missing.append("video_resume_url (must be a string)")

    if missing:
        raise ProfileConfigurationError(
            "Profile configuration is incomplete or unconfigured. "
            f"Missing/invalid field(s): {', '.join(missing)}. "
            "Copy config/profile.example.json to config/profile.private.json and populate your own details."
        )

    return profile


def load_profile(profile_path: str | Path | None = None) -> dict:
    """Load and validate a user profile from disk or PROFILE_JSON environment variable."""
    explicit_path = profile_path is not None or bool(os.getenv("PROFILE_PATH", "").strip())
    resolved_path = get_profile_path(profile_path)

    if resolved_path.exists():
        try:
            with resolved_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ProfileConfigurationError(
                f"Invalid JSON in profile file '{resolved_path.name}': {exc.msg} (line {exc.lineno})."
            ) from exc
        except OSError as exc:
            raise ProfileConfigurationError(
                f"Could not read profile file '{resolved_path.name}': {exc}."
            ) from exc
        return validate_profile(data)

    if not explicit_path:
        env_json = os.getenv("PROFILE_JSON", "").strip()
        if env_json:
            try:
                data = json.loads(env_json)
            except json.JSONDecodeError as exc:
                raise ProfileConfigurationError(
                    f"Invalid JSON in PROFILE_JSON environment variable: {exc.msg}."
                ) from exc
            return validate_profile(data)

    raise ProfileConfigurationError(
        f"Profile configuration not found at '{resolved_path}'. "
        "Copy config/profile.example.json to config/profile.private.json and populate your personal details "
        "(or set PROFILE_PATH / PROFILE_JSON)."
    )


def get_profile_fingerprint(profile: dict) -> str:
    """
    Compute a stable, non-sensitive fingerprint for the matching/filtering preferences
    of a profile so cached job state cannot leak across different user profiles.
    """
    payload = {
        "roles": sorted(r.lower() for r in get_profile_roles(profile)),
        "skills": sorted(s.lower() for s in get_profile_skills(profile)),
        "locations": sorted(l.lower() for l in get_profile_locations(profile)),
        "remote": get_profile_remote_prefs(profile),
        "excluded": sorted(c.lower() for c in get_profile_excluded_companies(profile)),
        "experience": get_profile_experience_prefs(profile),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _current_env_profile_key() -> tuple:
    """Return a cache key reflecting current PROFILE_PATH, PROFILE_JSON, and file mtime."""
    env_path = os.getenv("PROFILE_PATH", "").strip()
    env_json = os.getenv("PROFILE_JSON", "").strip()
    resolved = get_profile_path()
    mtime_ns = resolved.stat().st_mtime_ns if resolved.exists() else None
    return ("env", str(resolved), env_path, env_json, mtime_ns)


def set_active_profile(profile: dict | None, validate: bool = True) -> dict | None:
    """Set or clear the in-memory active profile (primarily for testing and programmatic callers)."""
    global _ACTIVE_PROFILE, _ACTIVE_PROFILE_KEY
    if profile is None:
        _ACTIVE_PROFILE = None
        _ACTIVE_PROFILE_KEY = None
        return None
    _ACTIVE_PROFILE = validate_profile(profile) if validate else profile
    _ACTIVE_PROFILE_KEY = ("explicit",)
    return _ACTIVE_PROFILE


def get_active_profile(profile: dict | None = None, reload: bool = False) -> dict:
    """Return the supplied profile or load, validate, and cache the active profile."""
    global _ACTIVE_PROFILE, _ACTIVE_PROFILE_KEY
    if profile is not None:
        return validate_profile(profile)
    if _ACTIVE_PROFILE is not None and not reload:
        if _ACTIVE_PROFILE_KEY == ("explicit",) or _ACTIVE_PROFILE_KEY == _current_env_profile_key():
            return _ACTIVE_PROFILE
    _ACTIVE_PROFILE = load_profile()
    _ACTIVE_PROFILE_KEY = _current_env_profile_key()
    return _ACTIVE_PROFILE


# ============================================================
# PIPELINE LIMITS & BATCHING (NO ARBITRARY CANDIDATE LOSS)
# ============================================================

# Number of jobs evaluated in a single Groq API request.
# All candidate batches are processed without truncation.
GROQ_BATCH_SIZE = 15

# Soft match floor for ranking (qualifying roles passing hard filters remain eligible)
MIN_MATCH_SCORE = 50

# Emergency ceiling only to prevent email payload overflow (no arbitrary 20-job cap)
MAX_EMAIL_SAFETY_CEILING = 200

# Raw candidate pool size for LinkedIn multi-partition collection
MAX_SCRAPED_JOBS = 300

# High-priority alert window and score
HIGH_PRIORITY_SCORE = 80
MEDIUM_PRIORITY_SCORE = 65
HIGH_PRIORITY_MAX_AGE_MINUTES = 180  # 3 hours (immediate alert)

# Operating freshness windows for reporting/ranking (sensible 24h window for hourly runs, 48h for ATS feeds)
FRESHNESS_WINDOW_HOURS = int(os.environ.get("FRESHNESS_WINDOW_HOURS", "24"))
FRESHNESS_WINDOW_MINUTES = int(
    os.environ.get("FRESHNESS_WINDOW_MINUTES", str(FRESHNESS_WINDOW_HOURS * 60))
)
ATS_FRESHNESS_WINDOW_HOURS = int(os.environ.get("ATS_FRESHNESS_WINDOW_HOURS", "48"))
ATS_FRESHNESS_WINDOW_MINUTES = int(
    os.environ.get("ATS_FRESHNESS_WINDOW_MINUTES", str(ATS_FRESHNESS_WINDOW_HOURS * 60))
)
MAX_JOB_AGE_DAYS = int(os.environ.get("MAX_JOB_AGE_DAYS", "14"))


# ============================================================
# ATS TARGET COMPANIES MAPPING
# ============================================================

ATS_TARGET_COMPANIES = {
    "greenhouse": [
        "gitlab",
        "stripe",
        "scaleai",
        "airtable",
        "databricks",
        "instacart",
        "figma",
        "anthropic",
    ],
    "lever": [
        "spotify",
        "palantir",
    ],
    "ashby": [
        "notion",
        "openai",
        "perplexity",
        "cursor",
        "ramp",
    ],
}


# ============================================================
# MULTI-SOURCE ACQUISITION CONFIGURATION
# ============================================================

# Polling intervals in minutes
POLL_INTERVAL_FAST_MINUTES = 3       # e.g., ATS endpoints, fast JSON APIs
POLL_INTERVAL_MEDIUM_MINUTES = 10    # e.g., remote boards, RSS feeds
POLL_INTERVAL_SLOW_MINUTES = 25      # e.g., LinkedIn Apify, heavy scraping

SOURCES_CONFIG = {
    # 1. Existing LinkedIn via Apify
    "linkedin": {
        "name": "LinkedIn (Apify)",
        "enabled": True,
        "category": "linkedin",
        "acquisition_method": "apify_actor",
        "polling_interval_minutes": int(os.getenv("POLL_INTERVAL_LINKEDIN", "25")),
        "max_results": int(os.getenv("MAX_SCRAPED_JOBS", str(MAX_SCRAPED_JOBS))),
    },
    # 2. Remote OK (Official JSON API)
    "remoteok": {
        "name": "Remote OK",
        "enabled": os.getenv("ENABLE_REMOTEOK", "true").lower() == "true",
        "category": "remote_board",
        "acquisition_method": "json_api",
        "polling_interval_minutes": int(os.getenv("POLL_INTERVAL_REMOTEOK", "3")),
        "max_results": int(os.getenv("MAX_SCRAPED_JOBS", str(MAX_SCRAPED_JOBS))),
        "endpoint": "https://remoteok.com/api",
    },
    # 3. Remotive (Official JSON API)
    "remotive": {
        "name": "Remotive",
        "enabled": os.getenv("ENABLE_REMOTIVE", "true").lower() == "true",
        "category": "remote_board",
        "acquisition_method": "json_api",
        "polling_interval_minutes": int(os.getenv("POLL_INTERVAL_REMOTIVE", "5")),
        "max_results": int(os.getenv("MAX_SCRAPED_JOBS", str(MAX_SCRAPED_JOBS))),
        "endpoint": "https://remotive.com/api/remote-jobs",
    },
    # 4. Working Nomads (Official JSON API)
    "workingnomads": {
        "name": "Working Nomads",
        "enabled": os.getenv("ENABLE_WORKINGNOMADS", "true").lower() == "true",
        "category": "remote_board",
        "acquisition_method": "json_api",
        "polling_interval_minutes": int(os.getenv("POLL_INTERVAL_WORKINGNOMADS", "10")),
        "max_results": int(os.getenv("MAX_SCRAPED_JOBS", str(MAX_SCRAPED_JOBS))),
        "endpoint": "https://www.workingnomads.com/api/exposed_jobs/",
    },
    # 5. We Work Remotely (RSS Feeds)
    "weworkremotely": {
        "name": "We Work Remotely",
        "enabled": os.getenv("ENABLE_WEWORKREMOTELY", "true").lower() == "true",
        "category": "remote_board",
        "acquisition_method": "rss_feed",
        "polling_interval_minutes": int(os.getenv("POLL_INTERVAL_WWR", "5")),
        "max_results": int(os.getenv("MAX_SCRAPED_JOBS", str(MAX_SCRAPED_JOBS))),
        "feeds": [
            "https://weworkremotely.com/categories/remote-programming-jobs.rss",
            "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
        ],
    },
    # 6. NoDesk (RSS Feed)
    "nodesk": {
        "name": "NoDesk",
        "enabled": os.getenv("ENABLE_NODESK", "true").lower() == "true",
        "category": "remote_board",
        "acquisition_method": "rss_feed",
        "polling_interval_minutes": int(os.getenv("POLL_INTERVAL_NODESK", "10")),
        "max_results": int(os.getenv("MAX_SCRAPED_JOBS", str(MAX_SCRAPED_JOBS))),
        "feed": "https://nodesk.co/remote-jobs/index.xml",
    },
    # 7. SkipTheDrive (Public feed / listings)
    "skipthedrive": {
        "name": "SkipTheDrive",
        "enabled": os.getenv("ENABLE_SKIPTHEDRIVE", "true").lower() == "true",
        "category": "remote_board",
        "acquisition_method": "rss_feed",
        "polling_interval_minutes": int(os.getenv("POLL_INTERVAL_SKIPTHEDRIVE", "20")),
        "max_results": int(os.getenv("MAX_SCRAPED_JOBS", str(MAX_SCRAPED_JOBS))),
        "feed": "https://www.skipthedrive.com/jobs/feed/",
    },
    # 8. Remote.co
    "remoteco": {
        "name": "Remote.co",
        "enabled": os.getenv("ENABLE_REMOTECO", "true").lower() == "true",
        "category": "remote_board",
        "acquisition_method": "rss_feed",
        "polling_interval_minutes": int(os.getenv("POLL_INTERVAL_REMOTECO", "20")),
        "max_results": int(os.getenv("MAX_SCRAPED_JOBS", str(MAX_SCRAPED_JOBS))),
        "feed": "https://remote.co/remote-jobs/developer/feed/",
    },
    # 9. Remote100K
    "remote100k": {
        "name": "Remote100K",
        "enabled": os.getenv("ENABLE_REMOTE100K", "true").lower() == "true",
        "category": "remote_board",
        "acquisition_method": "structured_web",
        "polling_interval_minutes": int(os.getenv("POLL_INTERVAL_REMOTE100K", "20")),
        "max_results": 50,
    },
    # 10. JustRemote
    "justremote": {
        "name": "JustRemote",
        "enabled": os.getenv("ENABLE_JUSTREMOTE", "true").lower() == "true",
        "category": "remote_board",
        "acquisition_method": "structured_web",
        "polling_interval_minutes": int(os.getenv("POLL_INTERVAL_JUSTREMOTE", "20")),
        "max_results": 50,
    },
    # 11. ATS: Greenhouse (Public Boards API)
    "greenhouse": {
        "name": "Greenhouse ATS",
        "enabled": os.getenv("ENABLE_GREENHOUSE", "true").lower() == "true",
        "category": "ats",
        "acquisition_method": "json_api",
        "polling_interval_minutes": int(os.getenv("POLL_INTERVAL_GREENHOUSE", "3")),
        "max_results": int(os.getenv("MAX_SCRAPED_JOBS", str(MAX_SCRAPED_JOBS))),
        "companies": ATS_TARGET_COMPANIES["greenhouse"],
    },
    # 12. ATS: Lever (Public Postings API)
    "lever": {
        "name": "Lever ATS",
        "enabled": os.getenv("ENABLE_LEVER", "true").lower() == "true",
        "category": "ats",
        "acquisition_method": "json_api",
        "polling_interval_minutes": int(os.getenv("POLL_INTERVAL_LEVER", "3")),
        "max_results": int(os.getenv("MAX_SCRAPED_JOBS", str(MAX_SCRAPED_JOBS))),
        "companies": ATS_TARGET_COMPANIES["lever"],
    },
    # 13. ATS: Ashby (Public Job Board API)
    "ashby": {
        "name": "Ashby ATS",
        "enabled": os.getenv("ENABLE_ASHBY", "true").lower() == "true",
        "category": "ats",
        "acquisition_method": "json_api",
        "polling_interval_minutes": int(os.getenv("POLL_INTERVAL_ASHBY", "3")),
        "max_results": int(os.getenv("MAX_SCRAPED_JOBS", str(MAX_SCRAPED_JOBS))),
        "companies": ATS_TARGET_COMPANIES["ashby"],
    },
    # 14. Indeed
    "indeed": {
        "name": "Indeed",
        "enabled": os.getenv("ENABLE_INDEED", "false").lower() == "true",
        "category": "aggregator",
        "acquisition_method": "official_api_or_feed",
        "polling_interval_minutes": int(os.getenv("POLL_INTERVAL_INDEED", "30")),
        "max_results": 20,
    },
    # 15. Wellfound
    "wellfound": {
        "name": "Wellfound",
        "enabled": os.getenv("ENABLE_WELLFOUND", "false").lower() == "true",
        "category": "aggregator",
        "acquisition_method": "official_api_or_feed",
        "polling_interval_minutes": int(os.getenv("POLL_INTERVAL_WELLFOUND", "30")),
        "max_results": 20,
    },
    # 16. Naukri
    "naukri": {
        "name": "Naukri",
        "enabled": os.getenv("ENABLE_NAUKRI", "false").lower() == "true",
        "category": "aggregator",
        "acquisition_method": "official_api_or_feed",
        "polling_interval_minutes": int(os.getenv("POLL_INTERVAL_NAUKRI", "30")),
        "max_results": 20,
    },
    # 17. Instahyre
    "instahyre": {
        "name": "Instahyre",
        "enabled": os.getenv("ENABLE_INSTAHYRE", "false").lower() == "true",
        "category": "aggregator",
        "acquisition_method": "official_api_or_feed",
        "polling_interval_minutes": int(os.getenv("POLL_INTERVAL_INSTAHYRE", "30")),
        "max_results": 20,
    },
    # 18. Cutshort
    "cutshort": {
        "name": "Cutshort",
        "enabled": os.getenv("ENABLE_CUTSHORT", "false").lower() == "true",
        "category": "aggregator",
        "acquisition_method": "official_api_or_feed",
        "polling_interval_minutes": int(os.getenv("POLL_INTERVAL_CUTSHORT", "30")),
        "max_results": 20,
    },
    # 19. Foundit
    "foundit": {
        "name": "Foundit",
        "enabled": os.getenv("ENABLE_FOUNDIT", "false").lower() == "true",
        "category": "aggregator",
        "acquisition_method": "official_api_or_feed",
        "polling_interval_minutes": int(os.getenv("POLL_INTERVAL_FOUNDIT", "30")),
        "max_results": 20,
    },
    # 20. Hirist
    "hirist": {
        "name": "Hirist",
        "enabled": os.getenv("ENABLE_HIRIST", "false").lower() == "true",
        "category": "aggregator",
        "acquisition_method": "official_api_or_feed",
        "polling_interval_minutes": int(os.getenv("POLL_INTERVAL_HIRIST", "30")),
        "max_results": 20,
    },
}
