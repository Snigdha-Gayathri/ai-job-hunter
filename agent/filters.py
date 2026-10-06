"""
Deterministic Hard Filters for AI Job Hunter:
1. Company Exclusion Filter (Infosys and variants)
2. Experience Filter (fresher / entry-level / 0-2 YOE, 1-3 YOE preferred, rejects 3+, 5+, 7+ YOE required)
3. Strict Work-Location Filter (Mumbai, Hyderabad, Bangalore/Bengaluru, Pune, Remote)
4. Role Relevance Classifier (AI/ML/GenAI/LLM/Agentic/Applied AI roles)

All hard filters are strictly deterministic. Zero LLM calls are made here.
"""

import re
from typing import Any
from config import EXCLUDED_COMPANIES, TARGET_ROLE_FAMILIES


# ============================================================
# 1. COMPANY EXCLUSION FILTER
# ============================================================

def normalize_company_name(name: str | None) -> str:
    """Normalize company name for robust exclusion checking."""
    if not name:
        return ""
    # Strip common punctuation and lowercase
    cleaned = re.sub(r"[^\w\s]", " ", str(name).lower())
    return " ".join(cleaned.split())


def is_company_excluded(company_name: str | None) -> tuple[bool, str]:
    """
    Deterministically checks if a company is in the exclusion list.
    Covers Infosys, Infosys Limited, Infosys BPM, and subsidiaries/variants.
    """
    if not company_name:
        return False, "Empty company name"

    norm = normalize_company_name(company_name)

    # Check against configured exclusions
    for exc in EXCLUDED_COMPANIES:
        exc_norm = normalize_company_name(exc)
        if exc_norm in norm or norm == exc_norm:
            return True, f"Excluded company matched: '{company_name}' ({exc})"

    # Generic check for Infosys variants
    if re.search(r"\binfosys\b", norm):
        return True, f"Excluded company matched: '{company_name}' (Infosys variant)"

    return False, "Company accepted"


# ============================================================
# 2. EXPERIENCE FILTER (Distinguish Required from Preferred)
# ============================================================

# Seniority title tokens that disqualify a role from being entry-level
SENIOR_TITLE_RE = re.compile(
    r"\b("
    r"senior|sr\.?|lead|staff|principal|director|head\s+of|vp\b|vice\s+president|"
    r"architect|manager|general\s+manager"
    r")\b",
    re.IGNORECASE,
)

# Counter-signals in title that negate seniority (e.g., "Associate", "Junior", "Intern")
JUNIOR_TITLE_OVERRIDE_RE = re.compile(
    r"\b(junior|associate|intern|internship|trainee|apprentice|fresher|graduate)\b",
    re.IGNORECASE,
)

# Explicit high experience required patterns (3+, 5+, 7+, 8+ YOE required, minimum 3 years, etc.)
HIGH_EXP_REQUIRED_RE = re.compile(
    r"\b("
    r"(?:minimum|at\s+least|min\.?)\s*(?:of\s+)?([3-9]|[1-9][0-9])\s*\+?\s*(?:years?|yrs?|yoe)|"
    r"([3-9]|[1-9][0-9])\s*\+?\s*(?:years?|yrs?|yoe)(?:\s+of\s+[\w\s]+)?\s+(?:required|mandatory|minimum|must\s+have)|"
    r"(?:required|mandatory|must\s+have)\s*(?::|\s+to\s+have)?\s*(?:a\s+)?(?:minimum\s+of\s+)?([3-9]|[1-9][0-9])\s*\+?\s*(?:years?|yrs?|yoe)|"
    r"([3-9]|[1-9][0-9])\s*\+\s*(?:years?|yrs?|yoe)\s+required"
    r")\b",
    re.IGNORECASE,
)

# High experience ranges (e.g., 5-8 years, 6-10 years, 3-5 years)
HIGH_EXP_RANGE_RE = re.compile(
    r"\b([3-9]|[1-9][0-9])\s*(?:to|-|–)\s*([0-9]+)\s*(?:years?|yrs?|yoe)\b",
    re.IGNORECASE,
)

# Explicit 3+, 5+, 7+ years pattern without range
UNQUALIFIED_HIGH_EXP_RE = re.compile(
    r"\b([3-9]|[1-9][0-9])\s*\+\s*(?:years?|yrs?|yoe)\b",
    re.IGNORECASE,
)

# Entry-level / Fresher / 0-2 YOE patterns to ACCEPT
FRESHER_ACCEPT_RE = re.compile(
    r"\b("
    r"freshers?\s+welcome|freshers?\s+may\s+apply|fresher|freshers|"
    r"no\s+experience\s+required|zero\s+experience|"
    r"entry[\s-]level|fresh\s+graduates?|new\s+graduates?|recent\s+graduates?|new\s+grads?|"
    r"0\s*(?:years?|yrs?|yoe)|"
    r"0\s*(?:to|-|–)\s*[12]\s*(?:years?|yrs?|yoe)|"
    r"0\s*-\s*[12]\s*(?:years?|yrs?|yoe)|"
    r"1\s*(?:to|-|–)\s*2\s*(?:years?|yrs?|yoe)|"
    r"1\s*-\s*2\s*(?:years?|yrs?|yoe)|"
    r"1\s*(?:years?|yrs?|yoe)|"
    r"2\s*(?:years?|yrs?|yoe)|"
    r"interns?|internships?|trainees?|apprentices?"
    r")\b",
    re.IGNORECASE,
)

# Preferred / Desirable experience patterns (e.g. "1-3 years preferred", "experience preferred")
PREFERRED_EXP_PATTERNS = [
    r"experience\s+preferred",
    r"1\s*(?:to|-|–)\s*3\s*(?:years?|yrs?|yoe)\s*(?:preferred|desirable|nice\s+to\s+have|plus)",
    r"(?:preferred|desirable|nice\s+to\s+have|plus)\s*:\s*1\s*(?:to|-|–)\s*3\s*(?:years?|yrs?|yoe)",
    r"1\s*(?:to|-|–)\s*3\s*(?:years?|yrs?|yoe)\s+is\s+(?:a\s+plus|preferred|desirable|an\s+advantage)",
    r"1\s*(?:to|-|–)\s*3\s*(?:years?|yrs?|yoe)\s+plus",
    r"1\s*(?:to|-|–)\s*3\s*(?:years?|yrs?|yoe)\s*;\s*freshers",
    r"freshers\s+with\s+strong\s+projects\s+may\s+apply",
]
PREFERRED_EXP_RE = re.compile(
    r"\b(" + "|".join(PREFERRED_EXP_PATTERNS) + r")\b",
    re.IGNORECASE,
)


def evaluate_experience_eligibility(job: dict) -> tuple[bool, str]:
    """
    Deterministic experience parser. Must be executed BEFORE Groq.

    ACCEPT:
    - '0-2 years', '0–2 years', '0-1 years', '1-2 years'
    - 'freshers welcome', 'freshers may apply', 'entry level', 'graduate', 'new graduate'
    - 'no experience required', 'experience preferred'
    - '1-3 years preferred', '1-3 years desirable', '1-3 years nice to have', '1-3 years plus'
    - '1-3 years preferred; freshers with strong projects may apply'

    REJECT:
    - Senior titles (Senior, Lead, Staff, Principal, Architect, Manager, etc.)
    - '3+ years required', 'minimum 3 years', 'at least 3 years'
    - '5+ years required', '7+ years required', '8+ years required'
    - '5-8 years required', '6-10 years required', '3-5 years'
    - High ranges (5-8, 6-10) or single >= 3+ years required
    """
    title = str(job.get("title") or "").strip()
    desc = str(job.get("description") or job.get("descriptionHtml") or "")
    full_text = f"{title}\n{desc}"
    full_text_lower = full_text.lower()

    # 1. Check title for senior/lead/staff/principal/architect/manager
    if SENIOR_TITLE_RE.search(title):
        # 'Member of Technical Staff' is standard non-hierarchical IC title at AI labs (OpenAI, Anthropic, Perplexity)
        is_mts = bool(re.search(r"\bmember\s+of\s+technical\s+staff\b|\bmts\b", title, re.IGNORECASE))
        is_senior_mts = bool(re.search(r"\b(senior|sr\.?|lead|principal)\s+(?:member\s+of\s+technical\s+staff|mts)\b", title, re.IGNORECASE))
        if is_mts and not is_senior_mts:
            pass  # Non-senior Member of Technical Staff allowed!
        elif not JUNIOR_TITLE_OVERRIDE_RE.search(title):
            return False, f"Senior role title detected: '{title}'"

    # 2. Check for explicit HIGH experience required (e.g. '3+ years required', 'minimum 3 years', 'at least 5 years')
    high_req = HIGH_EXP_REQUIRED_RE.search(full_text_lower)
    if high_req:
        matched_str = high_req.group(0).strip()
        # Verify it is not company age (e.g. 'over 5 years in business')
        start_pos = max(0, high_req.start() - 60)
        end_pos = min(len(full_text_lower), high_req.end() + 60)
        context = full_text_lower[start_pos:end_pos]
        if not re.search(r"\b(company|firm|organization|founded|established|in business|serving)\b", context):
            return False, f"Explicit high experience required: '{matched_str}'"

    # 3. Check for high experience ranges (e.g. 5-8 years, 6-10 years, 3-5 years)
    range_req = HIGH_EXP_RANGE_RE.search(full_text_lower)
    if range_req:
        start_val = int(range_req.group(1))
        matched_range = range_req.group(0).strip()
        start_pos = max(0, range_req.start() - 60)
        end_pos = min(len(full_text_lower), range_req.end() + 60)
        context = full_text_lower[start_pos:end_pos]
        if not re.search(r"\b(company|firm|organization|founded|established|in business|serving)\b", context):
            if start_val >= 3:
                return False, f"High experience range required: '{matched_range}'"

    # 4. Check for unqualified '3+ years', '5+ years', etc.
    for m in UNQUALIFIED_HIGH_EXP_RE.finditer(full_text_lower):
        matched_high = m.group(0).strip()
        start_pos = max(0, m.start() - 60)
        end_pos = min(len(full_text_lower), m.end() + 60)
        context = full_text_lower[start_pos:end_pos]
        if re.search(r"\b(company|firm|organization|founded|established|in business|serving)\b", context):
            continue
        # If explicitly marked as required or without fresher override
        is_preferred = bool(re.search(r"\b(preferred|desirable|nice\s+to\s+have|plus|optional)\b", context))
        if not is_preferred:
            return False, f"High experience requirement detected: '{matched_high}'"

    # 5. Check if 1-3 years is preferred / desirable / nice to have / plus
    if PREFERRED_EXP_RE.search(full_text_lower):
        return True, "1-3 YOE preferred / desirable / project-eligible role accepted"

    # 6. Check for 1-3 years in text with context
    one_to_three_match = re.search(r"\b(?:0|1)\s*(?:to|-|–)\s*3\s*(?:years?|yrs?|yoe)\b", full_text_lower)
    if one_to_three_match:
        start_pos = max(0, one_to_three_match.start() - 70)
        end_pos = min(len(full_text_lower), one_to_three_match.end() + 70)
        context = full_text_lower[start_pos:end_pos]
        is_preferred_ctx = bool(re.search(
            r"\b(preferred|desirable|nice\s+to\s+have|plus|bonus|advantage|ideal|optional)\b",
            context,
        ))
        has_fresher_allowance = bool(re.search(
            r"\b(freshers?|graduates?|entry[\s-]level|project|portfolio)\b",
            full_text_lower,
        ))
        if not is_preferred_ctx and not has_fresher_allowance:
            return False, f"Required 1-3 years experience without preferred/fresher qualifier: '{one_to_three_match.group(0)}'"
        return True, "1-3 YOE with preferred/fresher context accepted"

    # 7. Explicit fresher / entry-level signals
    if FRESHER_ACCEPT_RE.search(full_text_lower):
        return True, "Fresher / entry-level / 0-2 YOE signals matched"

    # 8. If no high experience is required and title is not senior, accept as entry-level compatible
    return True, "Compatible entry-level AI role (no conflicting high experience requirement)"


# ============================================================
# 3. STRICT WORK-LOCATION FILTER
# ============================================================

TARGET_CITIES = [
    "mumbai",
    "bombay",
    "hyderabad",
    "secunderabad",
    "bangalore",
    "bengaluru",
    "pune",
]

TARGET_CITY_RE = re.compile(
    r"\b(" + "|".join(TARGET_CITIES) + r")\b",
    re.IGNORECASE,
)

EXPLICIT_REMOTE_RE = re.compile(
    r"\b("
    r"remote\s*-\s*india|remote\s+within\s+india|remote\s*,\s*india|"
    r"remote|telecommute|wfh|worldwide|anywhere|work\s+from\s+home"
    r")\b",
    re.IGNORECASE,
)

NON_GENUINE_REMOTE_RE = re.compile(
    r"\b("
    r"india\s*[/,|-]\s*hybrid|"
    r"india\s*[/,|-]\s*flexible|"
    r"occasional(?:ly)?\s+remote|"
    r"remote\s+occasionally|"
    r"hybrid[,\s-]+location\s+flexible|"
    r"partial(?:ly)?\s+remote|"
    r"remote[- ]friendly|"
    r"remote\s+option(?:al)?|"
    r"work\s+from\s+office\s+with\s+remote\s+option|"
    r"work\s+from\s+home\s+days|"
    r"wfh\s+days|"
    r"days?\s+(?:remote|wfh)|"
    r"temporar(?:y|ily)\s+remote|"
    r"hybrid\s*[/,|-]\s*remote|"
    r"remote\s*[/,|-]\s*hybrid"
    r")\b",
    re.IGNORECASE,
)

FOREIGN_RESTRICTION_RE = re.compile(
    r"\b("
    r"us\s+only|usa\s+only|u\.s\.\s+only|united\s+states\s+only|uk\s+only|united\s+kingdom\s+only|"
    r"europe\s+only|canada\s+only|australia\s+only|emea\s+only|latam\s+only|north\s+america\s+only|"
    r"americas\s+only|germany\s+only|singapore\s+only|us\s+remote|remote\s+us|remote\s+usa|"
    r"remote\s+[\(-]\s*us\b|remote\s+[\(-]\s*usa\b|"
    r"must\s+reside\s+in\s+(?:the\s+)?(?:us|usa|united\s+states)"
    r")\b",
    re.IGNORECASE,
)

NON_TARGET_CITIES = [
    "noida", "delhi", "new delhi", "gurgaon", "gurugram", "chennai", "kolkata",
    "ahmedabad", "jaipur", "chandigarh", "kochi", "coimbatore",
    "indore", "surat", "vadodara", "bhubaneswar",
    "united states", "usa", "united kingdom", "uk", "london", "canada", "ontario", "toronto",
    "germany", "australia", "singapore", "japan", "tokyo", "ireland", "dublin",
    "france", "paris", "spain", "madrid", "switzerland", "netherlands",
]

NON_TARGET_CITY_RE = re.compile(
    r"\b(" + "|".join(re.escape(c) for c in NON_TARGET_CITIES) + r")\b",
    re.IGNORECASE,
)


def is_valid_work_location(
    job_or_location: dict | str | None,
    workplace: str | None = None,
) -> tuple[bool, str]:
    """
    STRICT WORK-LOCATION FILTER

    ACCEPT:
    - Bangalore / Bengaluru
    - Hyderabad
    - Mumbai
    - Pune
    - Remote
    - Remote - India
    - Remote within India
    - Mumbai / Hybrid
    - Bangalore / Hybrid
    - Hyderabad / Hybrid
    - Pune / Hybrid

    REJECT:
    - Noida, Delhi, Gurgaon, Gurugram, Chennai, Kolkata, Ahmedabad, Jaipur
    - United States, United Kingdom, etc.
    - 'India / Hybrid', 'India / Flexible'
    - 'Occasional remote', 'Remote occasionally', 'Hybrid, location flexible', 'Remote option'
    - 'India' alone without explicit remote
    - Remote boards cannot infer Remote unless listing explicitly says remote and eligible in India.
    """
    if isinstance(job_or_location, dict):
        loc = str(job_or_location.get("location") or "").strip()
        wp = str(
            job_or_location.get("workplace")
            or job_or_location.get("workplaceType")
            or workplace
            or ""
        ).strip()
    else:
        loc = str(job_or_location or "").strip()
        wp = str(workplace or "").strip()

    loc_lower = loc.lower()
    wp_lower = wp.lower()
    combined_lower = f"{loc_lower} {wp_lower}".strip()

    # 1. Foreign geographic restriction check (e.g. US Only, UK Only)
    if FOREIGN_RESTRICTION_RE.search(combined_lower) and "india" not in loc_lower:
        return False, f"Remote restricted to foreign region: '{loc}'"

    # 2. Check for non-genuine remote phrases (e.g. 'India / Hybrid', 'India / Flexible', 'Occasional remote')
    if NON_GENUINE_REMOTE_RE.search(combined_lower):
        # Unless a target city (e.g. Bangalore, Mumbai) is explicitly present
        if not TARGET_CITY_RE.search(loc_lower):
            return False, f"Non-genuine remote qualification: '{loc}'"

    # 3. Target City Check: Mumbai, Hyderabad, Bangalore/Bengaluru, Pune
    # Physical presence in target cities is accepted for onsite, hybrid, and remote
    city_match = TARGET_CITY_RE.search(loc_lower)
    if city_match:
        target_city = city_match.group(1).capitalize()
        if "hybrid" in combined_lower:
            return True, f"Target city matched: {target_city} (Hybrid role in target city accepted)"
        if "remote" in combined_lower:
            return True, f"Target city matched: {target_city} (Remote-eligible)"
        return True, f"Target city matched: {target_city}"

    # 4. Non-target city / foreign region check: Noida, Delhi, Chennai, US, Canada, Japan, etc.
    if NON_TARGET_CITY_RE.search(loc_lower):
        # Workplace='onsite' or 'hybrid' strictly overrides and rejects for non-target cities
        if wp_lower in ("onsite", "on-site", "hybrid"):
            return False, f"Non-target location with workplace='{wp}': '{loc}'"
        # If it explicitly specifies genuine Remote in India (e.g. 'Delhi NCR (Remote)', 'Remote - India')
        if re.search(r"\b(remote\s*-\s*india|remote\s+within\s+india|remote\s*,\s*india|india\s*\(remote\))\b", combined_lower):
            return True, f"Remote role within India accepted: '{loc}'"
        # If it's a non-target Indian city with explicit remote (e.g. 'Delhi NCR (Remote)')
        indian_non_targets = ["delhi", "new delhi", "noida", "gurgaon", "gurugram", "chennai", "kolkata", "ahmedabad", "jaipur"]
        if any(inc in loc_lower for inc in indian_non_targets) and EXPLICIT_REMOTE_RE.search(combined_lower) and not NON_GENUINE_REMOTE_RE.search(combined_lower):
            if not FOREIGN_RESTRICTION_RE.search(combined_lower):
                return True, f"Remote role from Indian city headquarters accepted: '{loc}'"
        return False, f"Non-target location: '{loc}'"

    # 5. Explicit Remote Work Location Check
    if EXPLICIT_REMOTE_RE.search(combined_lower):
        # Must not be foreign restricted
        if FOREIGN_RESTRICTION_RE.search(combined_lower) and "india" not in loc_lower:
            return False, f"Remote restricted to foreign region: '{loc}'"
        return True, f"Explicit remote work location matched: '{loc}'"

    # 6. Reject 'India' alone without explicit remote
    if loc_lower in ("india", "pan india", "anywhere in india"):
        return False, f"India-wide search without explicit remote: '{loc}'"

    # 7. Reject empty, ambiguous, or unspecified locations
    if not loc or loc_lower in ("", "none", "null", "unknown", "n/a", "unspecified", "tbd", "flexible"):
        return False, f"Missing or ambiguous location: '{loc}'"

    return False, f"Non-target location: '{loc}'"


# ============================================================
# 4. ROLE RELEVANCE CLASSIFIER
# ============================================================

AI_ROLE_PATTERNS = [
    # Full specified AI/ML/GenAI/LLM/Agentic/Applied AI family
    r"\bai\s+engineers?\b",
    r"\bai\s*/\s*ml\s+engineers?\b",
    r"\bai\s*-\s*ml\s+engineers?\b",
    r"\bmachine\s+learning\s+engineers?\b",
    r"\bml\s+engineers?\b",
    r"\bgenerative\s+ai\s+engineers?\b",
    r"\bgenai\s+engineers?\b",
    r"\bllm\s+engineers?\b",
    r"\bllm\s+application\s+engineers?\b",
    r"\bllm\s+application\s+developers?\b",
    r"\bagentic\s+ai\s+engineers?\b",
    r"\bai\s+agents?\s+engineers?\b",
    r"\bapplied\s+ai\s+engineers?\b",
    r"\bai\s+software\s+engineers?\b",
    r"\bai\s+application\s+engineers?\b",
    r"\bai\s+developers?\b",
    r"\bml\s+developers?\b",
    r"\bgenerative\s+ai\s+developers?\b",
    r"\bgenai\s+developers?\b",
    r"\bai\s+research\s+engineers?\b",
    r"\bai\s+research\s+scientists?\b",
    r"\bnlp\s+engineers?\b",
    r"\bcomputer\s+vision\s+engineers?\b",
    r"\bai\s+platform\s+engineers?\b",
    r"\bai\s+solutions\s+engineers?\b",
    r"\bai\s+automation\s+engineers?\b",
    r"\bai\s+product\s+engineers?\b",
    r"\bmachine\s+learning\s+scientists?\b",
    r"\bml\s+scientists?\b",
    r"\bjunior\s+ai\s+engineers?\b",
    r"\bjunior\s+ml\s+engineers?\b",
    r"\bassociate\s+ai\s+engineers?\b",
    r"\bassociate\s+ml\s+engineers?\b",
    r"\bgraduate\s+ai\s+engineers?\b",
    r"\bgraduate\s+ml\s+engineers?\b",
    r"\bgraduate\s+ai\s*/\s*ml\b",
    r"\bai\s+interns?\b",
    r"\bml\s+interns?\b",
    r"\bai\s*/\s*ml\s+interns?\b",
    r"\bgenai\s+interns?\b",
    r"\bllm\s+interns?\b",
    r"\bagentic\s+ai\s+interns?\b",
    r"\bapplied\s+ai\s+interns?\b",
    r"\bai\s+trainees?\b",
    r"\bml\s+trainees?\b",
    r"\bai\s+code\s+trainers?\b",
    # Compound AI titles: Software Engineer - Generative AI, Software Engineer, AI Platform, etc.
    r"\bsoftware\s+engineers?\s*[-/,:]\s*(?:generative\s+ai|genai|ai\s+platform|ai|ml|machine\s+learning|llm|agentic)\b",
    r"\bsoftware\s+engineers?,\s*(?:generative\s+ai|genai|ai\s+platform|ai|ml|machine\s+learning|llm|agentic)\b",
    r"\b(?:generative\s+ai|genai|ai|ml|machine\s+learning|llm)\s+software\s+engineers?\b",
    r"\bapplied\s+scientists?\s*[-/,:]\s*(?:machine\s+learning|ml|ai)\b",
    r"\bapplied\s+scientists?,\s*(?:machine\s+learning|ml|ai)\b",
    r"\bapplied\s+scientists?\b",
]

AI_ROLE_COMPILED = [re.compile(p, re.IGNORECASE) for p in AI_ROLE_PATTERNS]

AI_TITLE_KEYWORDS = [
    "ai engineer", "machine learning", "genai", "generative ai", "llm",
    "agentic ai", "applied ai", "nlp", "computer vision", "ai developer",
    "ml developer", "ai platform", "ai solutions", "ai product",
    "ai intern", "ml intern", "ai trainee", "ml trainee", "ai agent",
    "forward deployed engineer",
]

# Explicitly unrelated roles (Frontend, Java backend, QA, DevOps, HR, Sales)
UNRELATED_ROLES = [
    r"\bfront[\s-]?end\s+developers?\b",
    r"\bfront[\s-]?end\s+engineers?\b",
    r"\bback[\s-]?end\s+developers?\b",
    r"\bback[\s-]?end\s+engineers?\b",
    r"\bfull[\s-]?stack\s+developers?\b",
    r"\bfull[\s-]?stack\s+engineers?\b",
    r"\bjava\s+developers?\b",
    r"\bjava\s+backend\b",
    r"\b\.net\s+developers?\b",
    r"\bdotnet\s+developers?\b",
    r"\bqa\s+engineers?\b",
    r"\bqa\s+testers?\b",
    r"\bquality\s+assurance\b",
    r"\bmanual\s+testers?\b",
    r"\bautomation\s+testers?\b",
    r"\bdevops\s+engineers?\b",
    r"\bdevops\b",
    r"\bsite\s+reliability\s+engineers?\b",
    r"\bsre\b",
    r"\bsystem\s+administrators?\b",
    r"\bsales\b",
    r"\baccount\s+executives?\b",
    r"\bmarketing\b",
    r"\bhuman\s+resources\b",
    r"\bhr\s+executives?\b",
    r"\brecruiters?\b",
    r"\btalent\s+acquisition\b",
    r"\bfinance\b",
    r"\baccountants?\b",
]

UNRELATED_ROLES_COMPILED = [re.compile(p, re.IGNORECASE) for p in UNRELATED_ROLES]

AI_TOKEN_IN_TITLE_RE = re.compile(
    r"\b(ai|ml|genai|generative\s+ai|llm|agentic|machine\s+learning|deep\s+learning|nlp|cv)\b",
    re.IGNORECASE,
)


def is_role_relevant(title: str, description: str = "") -> tuple[bool, str]:
    """
    Classifies whether a role is genuinely relevant to entry-level AI/ML engineering.

    Crucial Rules:
    - Recognizes full AI/ML/GenAI/LLM/Agentic/Applied AI family (40+ titles).
    - Recognizes compound titles: 'Software Engineer - Generative AI', 'Software Engineer, AI Platform',
      'Applied Scientist - Machine Learning', 'AI Solutions Engineer', 'AI Product Engineer', etc.
    - Does NOT reject genuine AI jobs merely because 'Software Engineer' appears in the title.
    - Evaluates description context for neutral engineering titles.
    - Deterministically rejects purely unrelated roles (Frontend, Java, QA, DevOps, Sales, HR).
    """
    title_clean = str(title or "").strip()
    title_lower = title_clean.lower()

    # 1. Direct Pattern Match on Title
    for pattern in AI_ROLE_COMPILED:
        m = pattern.search(title_lower)
        if m:
            return True, f"Target AI role matched in title: '{m.group(0)}'"

    # 2. Check for AI specialization keywords in title
    for kw in AI_TITLE_KEYWORDS:
        if kw in title_lower:
            return True, f"AI specialization matched in title: '{kw}'"

    # 3. Check for unrelated disciplines (Frontend, Java, DevOps, Sales, etc.)
    for pattern in UNRELATED_ROLES_COMPILED:
        m = pattern.search(title_lower)
        if m:
            # If the title ALSO has an explicit AI token (e.g. 'Backend Engineer - AI Agents')
            if AI_TOKEN_IN_TITLE_RE.search(title_lower):
                return True, f"AI-specialized software role: '{title_clean}'"
            return False, f"Unrelated role detected: '{title_clean}' ({m.group(0)})"

    # 4. Neutral titles (e.g. 'Software Engineer', 'Research Intern', 'Member of Technical Staff')
    # Inspect technical description for AI/ML specialization
    desc_lower = str(description or "").lower()
    core_ai_signals = [
        "generative ai", "llm", "llms", "agentic", "rag", "pytorch",
        "langchain", "langgraph", "fine-tuning", "machine learning",
        "deep learning", "transformers", "hugging face", "computer vision",
        "vector database", "qdrant", "pinecone",
    ]
    matched_signals = [s for s in core_ai_signals if s in desc_lower]

    # If title is software/tech/intern and description has strong AI focus
    is_tech_title = any(t in title_lower for t in ["engineer", "developer", "scientist", "intern", "trainee", "member of technical staff", "mts"])
    if is_tech_title and len(matched_signals) >= 1:
        # Require either 1 primary signal or 2 general signals
        primary_signals = ["generative ai", "llm", "agentic", "rag", "pytorch", "machine learning", "deep learning"]
        has_primary = any(p in desc_lower for p in primary_signals)
        if has_primary or len(matched_signals) >= 2:
            return True, f"AI technical specialization verified via description ({len(matched_signals)} signals: {', '.join(matched_signals[:3])})"

    return False, f"Non-AI role: '{title_clean}'"
