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

APIFY_TOKEN = os.environ[
    "APIFY_API_TOKEN"
]

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

GMAIL_USERNAME = os.environ[
    "GMAIL_USERNAME"
]

GMAIL_APP_PASSWORD = os.environ[
    "GMAIL_APP_PASSWORD"
]
