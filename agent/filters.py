"""
Deterministic Hard Filters for AI Job Hunter:
1. Company Exclusion Filter (driven by active user profile)
2. Experience Filter (driven by active user profile, with entry-level defaults)
3. Strict Work-Location Filter (driven by active user profile locations & remote prefs)
4. Role Relevance Classifier (driven by active user profile target roles & skills)

All hard filters are strictly deterministic. Zero LLM calls are made here.
"""

import re
from config import (
    get_active_profile,
    get_profile_excluded_companies,
    get_profile_experience_prefs,
    get_profile_locations,
    get_profile_remote_prefs,
    get_profile_roles,
    get_profile_skills,
    profile_targets_ai_roles,
)


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


def is_company_excluded(
    company_name: str | None,
    profile: dict | None = None,
) -> tuple[bool, str]:
    """
    Deterministically checks if a company is in the active profile's exclusion list.
    """
    if not company_name:
        return False, "Empty company name"

    active_profile = get_active_profile(profile)
    excluded_companies = get_profile_excluded_companies(active_profile)
    if not excluded_companies:
        return False, "Company accepted"

    norm = normalize_company_name(company_name)
    if not norm:
        return False, "Empty company name"

    for exc in excluded_companies:
        exc_norm = normalize_company_name(exc)
        if not exc_norm:
            continue
        if norm == exc_norm or re.search(rf"\b{re.escape(exc_norm)}\b", norm):
            return True, f"Excluded company matched: '{company_name}' ({exc})"

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


def evaluate_experience_eligibility(
    job: dict,
    profile: dict | None = None,
) -> tuple[bool, str]:
    """
    Deterministic experience parser. Must be executed BEFORE Groq.

    Uses the active profile's experience_preferences when configured
    (e.g. max_required_yoe, exclude_senior_titles, accept_fresher).
    For entry-level profiles (max_required_yoe <= 2), enforces fresher / 0-2 YOE rules
    while distinguishing required vs preferred experience.
    """
    active_profile = get_active_profile(profile)
    exp_prefs = get_profile_experience_prefs(active_profile)
    max_yoe = exp_prefs.get("max_required_yoe")
    exclude_senior = exp_prefs.get("exclude_senior_titles", True)
    accept_fresher = exp_prefs.get("accept_fresher", True)

    title = str(job.get("title") or "").strip()
    desc = str(job.get("description") or job.get("descriptionHtml") or "")
    full_text = f"{title}\n{desc}"
    full_text_lower = full_text.lower()

    # 1. Check title for senior/lead/staff/principal/architect/manager if profile excludes senior titles
    if exclude_senior and SENIOR_TITLE_RE.search(title):
        # 'Member of Technical Staff' is standard non-hierarchical IC title at AI labs
        is_mts = bool(re.search(r"\bmember\s+of\s+technical\s+staff\b|\bmts\b", title, re.IGNORECASE))
        is_senior_mts = bool(re.search(r"\b(senior|sr\.?|lead|principal)\s+(?:member\s+of\s+technical\s+staff|mts)\b", title, re.IGNORECASE))
        if is_mts and not is_senior_mts:
            pass  # Non-senior Member of Technical Staff allowed
        elif not JUNIOR_TITLE_OVERRIDE_RE.search(title):
            return False, f"Senior role title detected: '{title}'"

    # If profile targets mid/senior roles with a higher YOE threshold (or unlimited)
    if max_yoe is None and not exclude_senior:
        return True, "Experience accepted by profile preference"

    effective_max_yoe = 2 if max_yoe is None else max_yoe

    if effective_max_yoe > 2:
        min_reject = effective_max_yoe + 1
        custom_req = re.search(
            rf"\b(?:minimum|at\s+least|min\.?|requires?)\s*(?:of\s+)?(\d+)\s*\+?\s*(?:years?|yrs?|yoe)\b",
            full_text_lower,
        )
        if custom_req and int(custom_req.group(1)) >= min_reject:
            return False, f"Required experience ({custom_req.group(1)}+ yrs) exceeds profile limit ({effective_max_yoe} yrs)"
        custom_range = re.search(
            r"\b(\d+)\s*(?:to|-|–)\s*(\d+)\s*(?:years?|yrs?|yoe)\b",
            full_text_lower,
        )
        if custom_range and int(custom_range.group(1)) >= min_reject:
            return False, f"Experience range ({custom_range.group(0)}) exceeds profile limit ({effective_max_yoe} yrs)"
        return True, f"Experience compatible with profile limit (<= {effective_max_yoe} YOE)"

    # Entry-level (0-2 YOE) deterministic checks
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
    if accept_fresher and FRESHER_ACCEPT_RE.search(full_text_lower):
        return True, "Fresher / entry-level / 0-2 YOE signals matched"

    # 8. If no high experience is required and title is not senior, accept as entry-level compatible
    return True, "Compatible entry-level role (no conflicting high experience requirement)"


# ============================================================
# 3. STRICT WORK-LOCATION FILTER
# ============================================================

CITY_ALIASES = {
    "mumbai": ["mumbai", "bombay"],
    "bombay": ["mumbai", "bombay"],
    "hyderabad": ["hyderabad", "secunderabad"],
    "secunderabad": ["hyderabad", "secunderabad"],
    "bangalore": ["bangalore", "bengaluru"],
    "bengaluru": ["bangalore", "bengaluru"],
    "gurgaon": ["gurgaon", "gurugram"],
    "gurugram": ["gurgaon", "gurugram"],
    "delhi": ["delhi", "new delhi", "delhi ncr"],
    "new delhi": ["delhi", "new delhi", "delhi ncr"],
}

COUNTRY_REGION_ALIASES = {
    "india": {"india", "in"},
    "united states": {"united states", "us", "usa", "u.s.", "north america", "americas"},
    "us": {"united states", "us", "usa", "u.s.", "north america", "americas"},
    "usa": {"united states", "us", "usa", "u.s.", "north america", "americas"},
    "united kingdom": {"united kingdom", "uk", "u.k.", "europe", "emea"},
    "uk": {"united kingdom", "uk", "u.k.", "europe", "emea"},
    "canada": {"canada", "north america", "americas"},
    "germany": {"germany", "europe", "emea"},
    "australia": {"australia", "apac"},
    "singapore": {"singapore", "apac"},
    "europe": {"europe", "emea"},
}

FOREIGN_COUNTRY_NAMES = [
    "united states", "usa", "united kingdom", "uk", "london", "canada", "ontario", "toronto",
    "germany", "berlin", "australia", "sydney", "melbourne", "singapore", "japan", "tokyo",
    "ireland", "dublin", "france", "paris", "spain", "madrid", "switzerland", "netherlands",
    "amsterdam", "india",
]

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

FOREIGN_RESTRICTION_PATTERNS = {
    "us": r"\b(?:us\s+only|usa\s+only|u\.s\.\s+only|united\s+states\s+only|us\s+remote|remote\s+us|remote\s+usa|remote\s+[\(-]\s*us\b|remote\s+[\(-]\s*usa\b|must\s+reside\s+in\s+(?:the\s+)?(?:us|usa|united\s+states))\b",
    "uk": r"\b(?:uk\s+only|united\s+kingdom\s+only)\b",
    "europe": r"\b(?:europe\s+only|emea\s+only)\b",
    "canada": r"\b(?:canada\s+only)\b",
    "australia": r"\b(?:australia\s+only)\b",
    "latam": r"\b(?:latam\s+only)\b",
    "north america": r"\b(?:north\s+america\s+only|americas\s+only)\b",
    "germany": r"\b(?:germany\s+only)\b",
    "singapore": r"\b(?:singapore\s+only)\b",
}

FOREIGN_RESTRICTION_COMPILED = {
    region: re.compile(pattern, re.IGNORECASE)
    for region, pattern in FOREIGN_RESTRICTION_PATTERNS.items()
}


def _expand_allowed_regions(remote_prefs: dict) -> set[str]:
    """Build lowercase set of allowed remote regions/countries for the active profile."""
    allowed: set[str] = set()
    for reg in remote_prefs.get("allowed_remote_regions") or []:
        r_low = str(reg).strip().lower()
        if r_low:
            allowed.add(r_low)
            allowed.update(COUNTRY_REGION_ALIASES.get(r_low, set()))
    home = str(remote_prefs.get("home_country") or "").strip().lower()
    if home:
        allowed.add(home)
        allowed.update(COUNTRY_REGION_ALIASES.get(home, set()))
    return allowed


def _is_foreign_restriction_for_profile(
    combined_lower: str,
    loc_lower: str,
    allowed_regions: set[str],
) -> bool:
    """Return True if location has a regional restriction incompatible with the profile."""
    for region, pattern in FOREIGN_RESTRICTION_COMPILED.items():
        if pattern.search(combined_lower):
            if region in allowed_regions:
                continue
            # Allow if an explicitly allowed specific country also appears in location (e.g. 'Remote - India')
            specific_allowed = allowed_regions - {"worldwide", "anywhere", "global"}
            if any(re.search(rf"\b{re.escape(r)}\b", loc_lower) for r in specific_allowed):
                continue
            return True
    return False


def _build_target_city_regex(target_locations: list[str]) -> re.Pattern | None:
    """Compile target city pattern from active profile locations with standard city aliases."""
    expanded: list[str] = []
    seen: set[str] = set()
    remote_tokens = {"remote", "worldwide", "anywhere", "global", "wfh"}

    for loc in target_locations:
        loc_low = str(loc or "").strip().lower()
        if not loc_low or loc_low in remote_tokens:
            continue
        variants = CITY_ALIASES.get(loc_low, [loc_low])
        for v in variants:
            if v not in seen:
                seen.add(v)
                expanded.append(re.escape(v))

    if not expanded:
        return None
    return re.compile(r"\b(" + "|".join(expanded) + r")\b", re.IGNORECASE)


def is_valid_work_location(
    job_or_location: dict | str | None,
    workplace: str | None = None,
    profile: dict | None = None,
) -> tuple[bool, str]:
    """
    PROFILE-DRIVEN STRICT WORK-LOCATION FILTER

    Evaluates job location and workplace metadata against the active user profile's
    target_locations and remote_preferences while preserving strict discrimination
    between genuine remote, hybrid, onsite, and foreign-restricted postings.
    """
    active_profile = get_active_profile(profile)
    target_locations = get_profile_locations(active_profile)
    remote_prefs = get_profile_remote_prefs(active_profile)
    accept_remote = bool(remote_prefs.get("accept_remote", False))
    accept_hybrid = bool(remote_prefs.get("accept_hybrid_in_target_cities", True))
    allowed_regions = _expand_allowed_regions(remote_prefs)
    home_country = str(remote_prefs.get("home_country") or "").strip().lower()
    target_city_re = _build_target_city_regex(target_locations)

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
    if _is_foreign_restriction_for_profile(combined_lower, loc_lower, allowed_regions):
        return False, f"Remote restricted to foreign region: '{loc}'"

    # 2. Check for non-genuine remote phrases (e.g. 'India / Hybrid', 'Occasional remote')
    if NON_GENUINE_REMOTE_RE.search(combined_lower):
        if not (target_city_re and target_city_re.search(loc_lower)):
            return False, f"Non-genuine remote qualification: '{loc}'"

    # 3. Target City Check from active profile
    if target_city_re:
        city_match = target_city_re.search(loc_lower)
        if city_match:
            target_city = city_match.group(1).capitalize()
            if "hybrid" in combined_lower:
                if accept_hybrid:
                    return True, f"Target city matched: {target_city} (Hybrid role in target city accepted)"
                return False, f"Hybrid role in {target_city} rejected by profile preference"
            if "remote" in combined_lower:
                return True, f"Target city matched: {target_city} (Remote-eligible)"
            return True, f"Target city matched: {target_city}"

    # 4. Non-target physical workplace check: onsite/hybrid outside target cities is rejected
    if wp_lower in ("onsite", "on-site", "hybrid"):
        return False, f"Non-target location with workplace='{wp}': '{loc}'"

    # 5. Explicit Remote Work Location Check
    if EXPLICIT_REMOTE_RE.search(combined_lower):
        if not accept_remote:
            return False, f"Remote-only location rejected by profile preferences: '{loc}'"
        if _is_foreign_restriction_for_profile(combined_lower, loc_lower, allowed_regions):
            return False, f"Remote restricted to foreign region: '{loc}'"
        # Reject remote explicitly tied to an unallowed foreign country unless global/worldwide
        is_global_remote = any(g in combined_lower for g in ("worldwide", "anywhere", "global"))
        if allowed_regions and not is_global_remote and wp_lower != "remote":
            for country in FOREIGN_COUNTRY_NAMES:
                if re.search(rf"\b{re.escape(country)}\b", loc_lower) and country not in allowed_regions:
                    return False, f"Remote role tied to non-target region '{country}': '{loc}'"
        return True, f"Explicit remote work location matched: '{loc}'"

    # 6. Reject generic country-only strings without explicit remote or target city
    country_only_tokens = {"india", "pan india", "anywhere in india"}
    if home_country:
        country_only_tokens.add(home_country)
    if loc_lower in country_only_tokens:
        return False, f"Country-wide location without explicit remote: '{loc}'"

    # 7. Reject empty, ambiguous, or unspecified locations
    if not loc or loc_lower in ("", "none", "null", "unknown", "n/a", "unspecified", "tbd", "flexible", "multiple locations", "hybrid"):
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

# Common non-target discipline patterns (skipped dynamically if included in user's profile)
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


def _compile_role_pattern(role: str) -> re.Pattern:
    """Compile a flexible regex for a configured target role or role family."""
    escaped = re.escape(role.strip().lower())
    # Allow flexible whitespace or slash separators
    escaped = re.sub(r"\\ ", r"\\s+", escaped)
    escaped = escaped.replace(r"\/", r"\s*/\s*")
    # Allow plural 's' on trailing noun
    if escaped.endswith(("engineer", "developer", "scientist", "intern", "trainee", "analyst", "architect", "specialist")):
        escaped = escaped + "s?"
    return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)


def _skill_in_text(skill: str, text_lower: str) -> bool:
    """Safely check if a skill token appears in text without partial-word false positives."""
    s = skill.strip().lower()
    if not s:
        return False
    return bool(re.search(rf"(?<!\w){re.escape(s)}(?!\w)", text_lower))


def is_role_relevant(
    title: str,
    description: str = "",
    profile: dict | None = None,
) -> tuple[bool, str]:
    """
    Classifies whether a role is relevant to the active user profile's target roles,
    role families, and skills.
    """
    active_profile = get_active_profile(profile)
    roles = get_profile_roles(active_profile)
    skills = get_profile_skills(active_profile)
    excluded_roles = [
        str(r).strip().lower()
        for r in (active_profile.get("excluded_roles") or [])
        if str(r).strip()
    ]
    targets_ai = profile_targets_ai_roles(active_profile)

    title_clean = str(title or "").strip()
    title_lower = title_clean.lower()

    # 0. Explicit excluded roles from profile
    for exc_role in excluded_roles:
        if _compile_role_pattern(exc_role).search(title_lower):
            return False, f"Excluded role matched: '{title_clean}' ({exc_role})"

    # 1. Direct pattern match against active profile's target roles & role families
    for role in roles:
        m = _compile_role_pattern(role).search(title_lower)
        if m:
            return True, f"Target role matched in title: '{m.group(0)}'"

    # 1b. If profile targets AI/ML roles, apply comprehensive AI role family patterns
    if targets_ai:
        for pattern in AI_ROLE_COMPILED:
            m = pattern.search(title_lower)
            if m:
                return True, f"Target AI role matched in title: '{m.group(0)}'"

        for kw in AI_TITLE_KEYWORDS:
            if kw in title_lower:
                return True, f"AI specialization matched in title: '{kw}'"

    # 2. Check for unrelated disciplines (skipping any discipline targeted by the active profile)
    roles_lower_joined = " ".join(roles).lower()
    for pattern in UNRELATED_ROLES_COMPILED:
        if pattern.search(roles_lower_joined):
            continue
        m = pattern.search(title_lower)
        if m:
            if targets_ai and AI_TOKEN_IN_TITLE_RE.search(title_lower):
                return True, f"AI-specialized software role: '{title_clean}'"
            # Also check if a profile skill/role token explicitly specializes the title (e.g. 'Java Backend Engineer' -> 'Java')
            if any(_skill_in_text(r, title_lower) for r in roles):
                return True, f"Profile-aligned specialized role: '{title_clean}'"
            return False, f"Unrelated role detected: '{title_clean}' ({m.group(0)})"

    # 3. Neutral tech titles (e.g. 'Software Engineer', 'Research Intern', 'Member of Technical Staff')
    # Inspect technical description for active profile's technical specialization
    desc_lower = str(description or "").lower()
    is_tech_title = any(
        t in title_lower
        for t in [
            "engineer", "developer", "scientist", "intern", "trainee",
            "member of technical staff", "mts", "analyst", "architect", "specialist",
        ]
    )

    if is_tech_title:
        if targets_ai:
            core_ai_signals = [
                "generative ai", "llm", "llms", "agentic", "rag", "pytorch",
                "langchain", "langgraph", "fine-tuning", "machine learning",
                "deep learning", "transformers", "hugging face", "computer vision",
                "vector database", "qdrant", "pinecone",
            ]
            matched_signals = [s for s in core_ai_signals if _skill_in_text(s, desc_lower)]
            primary_signals = ["generative ai", "llm", "llms", "agentic", "rag", "pytorch", "machine learning", "deep learning"]
            has_primary = any(_skill_in_text(p, desc_lower) for p in primary_signals)
            if matched_signals and (has_primary or len(matched_signals) >= 2):
                return True, f"AI technical specialization verified via description ({len(matched_signals)} signals: {', '.join(matched_signals[:3])})"
        else:
            matched_skills = [s for s in skills if _skill_in_text(s, desc_lower) or _skill_in_text(s, title_lower)]
            if len(matched_skills) >= 2 or (len(skills) <= 3 and len(matched_skills) >= 1):
                return True, f"Profile technical specialization verified via description ({len(matched_skills)} skills: {', '.join(matched_skills[:3])})"

    return False, f"Non-target role: '{title_clean}'"
