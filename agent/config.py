import os
from pathlib import Path


# ============================================================
# PROJECT PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

STATE_DIR = BASE_DIR / "state"
ASSETS_DIR = BASE_DIR / "assets"

RECRUITER_STATE_FILE = (
    STATE_DIR / "recruiters.json"
)

OUTREACH_STATE_FILE = (
    STATE_DIR / "outreach_history.json"
)

RESUME_PATH = Path(
    os.getenv(
        "RESUME_PATH",
        str(
            ASSETS_DIR
            / "Snigdha_Gayathri_Resume.pdf"
        ),
    )
)


# ============================================================
# APIFY
# ============================================================

APIFY_TOKEN = os.getenv(
    "APIFY_API_TOKEN",
    "",
)

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
# CANDIDATE PROFILE
# ============================================================

CANDIDATE_NAME = "Snigdha Gayathri"

CANDIDATE_ROLE = (
    "AI/ML Engineer | Generative AI | "
    "LLMs | RAG | Agentic AI"
)

CANDIDATE_SKILLS = [
    "Python",
    "Machine Learning",
    "Deep Learning",
    "PyTorch",
    "TensorFlow",
    "scikit-learn",
    "LLMs",
    "Generative AI",
    "RAG",
    "Agentic AI",
    "LangChain",
    "LangGraph",
    "Multi-Agent Systems",
    "FastAPI",
    "Vector Databases",
    "Qdrant",
    "Pinecone",
    "Neo4j",
    "Embeddings",
    "Hybrid Search",
    "Reranking",
    "Hugging Face",
    "AI Evaluation",
    "MLOps",
]

TARGET_ROLES = [
    "AI Engineer",
    "AI/ML Engineer",
    "Junior AI Engineer",
    "Machine Learning Engineer",
    "ML Engineer",
    "Generative AI Engineer",
    "LLM Engineer",
    "Agentic AI Engineer",
    "Applied AI Engineer",
]

RELEVANT_PROJECTS = [
    "Agentic Placement RAG",
    "Enterprise Knowledge Intelligence Platform",
    "LLM Inference Optimization Lab",
    "Deep Learning Performance Profiler",
    "Smart Shelf AI",
]


# ============================================================
# VIDEO RESUME
# ============================================================

VIDEO_RESUME_URL = os.getenv(
    "VIDEO_RESUME_URL",
    "",
)


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
# COMPANY EXCLUSIONS
# ============================================================

# Companies explicitly excluded from all processing and alerts
EXCLUDED_COMPANIES = [
    "infosys",
    "infosys limited",
    "infosys bpm",
    "infosys bpm limited",
    "infosys technologies",
    "infosys consulting",
    "edgeverve",  # Infosys subsidiary
]


# ============================================================
# JOB TARGETING (ROLES, LOCATIONS & EXPERIENCE)
# ============================================================

TARGET_ROLE_FAMILIES = [
    "AI Engineer",
    "AI/ML Engineer",
    "Machine Learning Engineer",
    "ML Engineer",
    "Generative AI Engineer",
    "GenAI Engineer",
    "LLM Engineer",
    "LLM Application Engineer",
    "Agentic AI Engineer",
    "AI Agent Engineer",
    "Applied AI Engineer",
    "AI Software Engineer",
    "AI Platform Engineer",
    "AI Solutions Engineer",
    "AI Research Engineer",
    "NLP Engineer",
    "Computer Vision Engineer",
    "Machine Learning Scientist",
    "AI Developer",
    "ML Developer",
    "Generative AI Developer",
    "AI Automation Engineer",
    "AI Product Engineer",
    "AI Intern",
    "ML Intern",
    "AI/ML Intern",
    "GenAI Intern",
    "LLM Intern",
    "Agentic AI Intern",
    "Applied AI Intern",
    "AI Research Intern",
    "AI Trainee",
    "ML Trainee",
    "Graduate AI Engineer",
    "Graduate ML Engineer",
    "Junior AI Engineer",
    "Junior ML Engineer",
    "Associate AI Engineer",
    "Associate ML Engineer",
]

TARGET_LOCATIONS = [
    "mumbai",
    "hyderabad",
    "bangalore",
    "bengaluru",
    "pune",
    "remote",
]

TARGET_EXPERIENCE_KEYWORDS = [
    "fresher",
    "freshers",
    "fresh graduate",
    "new graduate",
    "recent graduate",
    "0 years",
    "0 year",
    "0-1 years",
    "0–1 years",
    "0-2 years",
    "0–2 years",
    "0 to 1 years",
    "0 to 2 years",
    "1 year",
    "1-2 years",
    "1–2 years",
    "entry level",
    "entry-level",
    "graduate",
    "new grad",
    "junior",
    "associate",
    "intern",
    "internship",
    "trainee",
    "student",
    "apprentice",
]


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

# Operating freshness windows for reporting/ranking
FRESHNESS_WINDOW_MINUTES = int(os.environ.get("FRESHNESS_WINDOW_MINUTES", "90"))
ATS_FRESHNESS_WINDOW_HOURS = int(os.environ.get("ATS_FRESHNESS_WINDOW_HOURS", "48"))
ATS_FRESHNESS_WINDOW_MINUTES = int(
    os.environ.get("ATS_FRESHNESS_WINDOW_MINUTES", str(ATS_FRESHNESS_WINDOW_HOURS * 60))
)
FRESHNESS_WINDOW_HOURS = ATS_FRESHNESS_WINDOW_HOURS


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

