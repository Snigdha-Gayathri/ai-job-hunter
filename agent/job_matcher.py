import json
import os
import re
import time
from typing import Any

import requests


GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

MODEL_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "openai/gpt-oss-20b"

# Hard cap.
# Only ONE Groq request is made per workflow run.
MAX_AI_JOBS = 15

# Local filtering keywords.
AI_ROLE_KEYWORDS = {
    "ai engineer",
    "artificial intelligence engineer",
    "machine learning engineer",
    "ml engineer",
    "machine learning",
    "ml engineer",
    "generative ai",
    "genai",
    "llm engineer",
    "llm",
    "rag",
    "retrieval augmented generation",
    "agentic ai",
    "ai agent",
    "applied ai",
    "deep learning",
    "nlp engineer",
    "computer vision",
    "ai/ml",
    "ai / ml",
}

TECHNICAL_KEYWORDS = {
    "python",
    "pytorch",
    "tensorflow",
    "keras",
    "scikit-learn",
    "hugging face",
    "transformers",
    "llm",
    "rag",
    "langchain",
    "langgraph",
    "vector database",
    "vector search",
    "embeddings",
    "qdrant",
    "pinecone",
    "neo4j",
    "fastapi",
    "docker",
    "machine learning",
    "deep learning",
    "generative ai",
    "artificial intelligence",
}

SENIOR_TERMS = {
    "senior",
    "sr.",
    "sr ",
    "lead",
    "staff",
    "principal",
    "manager",
    "director",
    "architect",
    "head of",
    "vp ",
    "vice president",
}

FRESHER_TERMS = {
    "fresher",
    "fresh graduate",
    "graduate",
    "entry level",
    "entry-level",
    "junior",
    "associate",
    "0-1 years",
    "0-2 years",
    "0 to 1 years",
    "0 to 2 years",
    "new grad",
}


CANDIDATE_PROFILE = """
Candidate: Snigdha Gayathri

Education:
- B.Tech in Computer Science and Engineering, AI & ML
- Graduation year: 2026
- CGPA: 8.30

Target roles:
- AI Engineer
- Junior AI Engineer
- Machine Learning Engineer
- ML Engineer
- Generative AI Engineer
- LLM Engineer
- Agentic AI Engineer
- Applied AI Engineer
- AI/ML Engineer
- Entry-level AI/ML roles

Programming:
- Python
- Java
- C++

Machine Learning / Deep Learning:
- PyTorch
- TensorFlow
- Keras
- scikit-learn
- NumPy
- Pandas
- Hugging Face Transformers

Generative AI:
- LLMs
- RAG
- Agentic AI
- LangChain
- LangGraph
- multi-agent systems
- tool calling
- structured outputs
- prompt engineering
- embeddings
- hybrid retrieval
- vector search
- reranking

Databases:
- Qdrant
- Pinecone
- Supabase
- Neo4j

Backend / Engineering:
- FastAPI
- React
- Next.js
- TypeScript
- Docker

Relevant projects:
- Agentic Placement RAG
- Enterprise Knowledge Intelligence Platform (EKIP)
- LLM Inference Optimization Lab
- Deep Learning Performance Profiler
- Smart Shelf AI

Additional technical experience:
- LLM inference optimization
- model quantization
- FP32 / FP16 / BF16
- 4-bit / 8-bit quantization
- transformer architectures
- GPU performance profiling
- benchmarking
- AI evaluation
- MLOps concepts
"""


def extract_json(text: str) -> Any:
    """
    Extract JSON from an LLM response.

    Handles:
    - plain JSON
    - markdown code fences
    - extra text surrounding the JSON
    """

    text = text.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text)
        text = re.sub(r"```$", "", text)
        text = text.strip()

    # First try parsing the complete response.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fall back to locating the first JSON object/array.
    object_start = text.find("{")
    object_end = text.rfind("}")

    if object_start != -1 and object_end != -1:
        return json.loads(text[object_start:object_end + 1])

    array_start = text.find("[")
    array_end = text.rfind("]")

    if array_start != -1 and array_end != -1:
        return json.loads(text[array_start:array_end + 1])

    raise ValueError(
        f"Model did not return valid JSON:\n{text[:3000]}"
    )


def get_job_text(job: dict) -> str:
    """
    Build one normalized text representation of a job.
    """

    title = str(job.get("title") or "")
    company = str(job.get("companyName") or "")
    location = str(job.get("location") or "")

    description = (
        job.get("description")
        or job.get("descriptionHtml")
        or ""
    )

    return (
        f"{title}\n"
        f"{company}\n"
        f"{location}\n"
        f"{str(description)[:12000]}"
    ).lower()


def local_score_job(job: dict) -> dict:
    """
    Cheap deterministic pre-filter.

    This function performs ZERO API calls.

    It exists to prevent irrelevant jobs from reaching Groq.
    """

    title = str(job.get("title") or "").lower()
    text = get_job_text(job)

    score = 0
    reasons = []

    # Strong signal: title is directly related to AI/ML.
    title_matches = [
        keyword
        for keyword in AI_ROLE_KEYWORDS
        if keyword in title
    ]

    if title_matches:
        score += 45
        reasons.append("AI/ML role detected in title")

    # Technical overlap.
    technical_matches = [
        keyword
        for keyword in TECHNICAL_KEYWORDS
        if keyword in text
    ]

    technical_points = min(len(technical_matches) * 5, 35)

    if technical_points:
        score += technical_points
        reasons.append(
            f"{len(technical_matches)} relevant technical signals"
        )

    # Fresher-friendly language.
    fresher_matches = [
        keyword
        for keyword in FRESHER_TERMS
        if keyword in text
    ]

    if fresher_matches:
        score += 15
        reasons.append("Entry-level/fresher language detected")

    # Seniority penalty.
    senior_matches = [
        keyword
        for keyword in SENIOR_TERMS
        if keyword in title
    ]

    if senior_matches:
        score -= 70
        reasons.append("Senior-level title detected")

    # Explicit high experience requirement.
    experience_patterns = [
        r"\b([5-9]|[1-9][0-9])\+?\s*years?\b",
        r"\b([5-9]|[1-9][0-9])\s*-\s*([5-9]|[1-9][0-9])\s*years?\b",
    ]

    high_experience = any(
        re.search(pattern, text)
        for pattern in experience_patterns
    )

    if high_experience:
        score -= 25
        reasons.append("High experience requirement detected")

    score = max(0, min(score, 100))

    return {
        "local_score": score,
        "local_reasons": reasons,
        "technical_matches": technical_matches[:10],
    }


def locally_filter_jobs(
    jobs: list[dict],
    minimum_score: int = 20,
) -> list[dict]:
    """
    Apply local scoring and return only plausible AI/ML jobs.

    No external API calls are made here.
    """

    candidates = []

    for job in jobs:
        result = local_score_job(job)

        job["local_score"] = result["local_score"]
        job["local_reasons"] = result["local_reasons"]

        if result["local_score"] >= minimum_score:
            candidates.append(job)

    candidates.sort(
        key=lambda job: job.get("local_score", 0),
        reverse=True,
    )

    print(
        f"Local filter: {len(jobs)} jobs -> "
        f"{len(candidates)} AI/ML candidates"
    )

    # Critical API-cost control.
    candidates = candidates[:MAX_AI_JOBS]

    print(
        f"Sending maximum {len(candidates)} jobs "
        f"to the single Groq request."
    )

    return candidates


def build_batch_prompt(jobs: list[dict]) -> str:
    """
    Construct one prompt containing multiple jobs.

    This replaces one prompt per job.
    """

    job_blocks = []

    for index, job in enumerate(jobs, start=1):
        title = job.get("title") or "Unknown title"
        company = job.get("companyName") or "Unknown company"
        location = job.get("location") or "Unknown location"

        description = (
            job.get("description")
            or job.get("descriptionHtml")
            or ""
        )

        description = str(description)[:10000]

        job_blocks.append(
            f"""
JOB {index}

Title:
{title}

Company:
{company}

Location:
{location}

Description:
{description}
"""
        )

    return f"""
You are an expert technical recruiter.

Evaluate ALL jobs below against the candidate profile.

Do not evaluate them independently with separate responses.
Return one JSON array containing exactly one result for each job.

# CANDIDATE PROFILE

{CANDIDATE_PROFILE}

# EVALUATION RULES

1. The candidate is a 2026 graduate targeting entry-level and
   junior AI/ML engineering roles.

2. Penalize roles that explicitly require significant prior
   professional experience.

3. Strongly penalize:
   - Senior
   - Lead
   - Staff
   - Principal
   - Manager
   - Director
   - Architect

4. Treat "0-2 years" and "0-1 years" as compatible.

5. Do NOT reject a role simply because the candidate does not
   have every preferred technology.

6. Prioritize actual technical overlap over keyword overlap.

7. Strong positive signals include:
   - Python
   - Machine Learning
   - Deep Learning
   - PyTorch
   - TensorFlow
   - Hugging Face
   - LLMs
   - RAG
   - AI agents
   - LangChain
   - LangGraph
   - vector databases
   - embeddings
   - FastAPI
   - inference
   - MLOps
   - AI evaluation
   - Docker

8. Projects count as legitimate evidence of technical ability.

9. Distinguish between required qualifications,
   preferred qualifications, and responsibilities.

10. A role should score highly when the candidate could
    reasonably perform the work despite being a fresher.

11. A job requiring unrelated enterprise software skills
    should score lower.

12. Do not fabricate candidate experience.

13. Be conservative and honest.

14. Do not give a high score merely because the job contains
    many AI keywords.

15. If the role is primarily software engineering, data
    engineering, QA, support, business analysis, or another
    non-AI discipline with only minor AI exposure, reduce
    the score.

16. If the job explicitly requires more experience than a
    fresh graduate can reasonably satisfy, reflect that heavily.

17. Evaluate actual responsibilities, not just title.

18. A strong project match is valuable evidence, but projects
    must not be presented as professional employment.

# SCORING

90-100:
Exceptional match.

80-89:
Strong match.

75-79:
Good match.

60-74:
Moderate match.

Below 60:
Weak match.

# REQUIRED OUTPUT

Return ONLY a valid JSON array.

Use exactly this structure for every job:

[
  {{
    "job_index": 1,
    "match_score": 0,
    "qualification": "STRONG_MATCH",
    "experience_fit": "GOOD",
    "technical_fit": 0,
    "role_fit": 0,
    "key_matches": [],
    "missing_requirements": [],
    "concerns": [],
    "reason": ""
  }}
]

qualification MUST be one of:

STRONG_MATCH
GOOD_MATCH
MODERATE_MATCH
WEAK_MATCH

experience_fit MUST be one of:

EXCELLENT
GOOD
MODERATE
POOR

IMPORTANT:
- Include exactly one result for every job.
- job_index must correspond to the JOB number.
- Never invent experience.
- Return JSON only.

# JOBS

{"".join(job_blocks)}
"""


def score_jobs_batch(jobs: list[dict]) -> list[dict]:
    """
    Score multiple jobs with ONE Groq API request.

    This is the primary AI matching path.
    """

    if not jobs:
        return []

    if not GROQ_API_KEY:
        print("GROQ_API_KEY is not configured.")
        return local_fallback_results(jobs)

    prompt = build_batch_prompt(jobs)

    print(
        f"Sending ONE Groq batch request for {len(jobs)} jobs..."
    )

    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a precise technical recruiting "
                    "and job matching engine. "
                    "Return only valid JSON when requested."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "temperature": 0.1,
        "max_tokens": 4000,
    }

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            MODEL_URL,
            headers=headers,
            json=payload,
            timeout=180,
        )

        print(
            f"Groq HTTP status: {response.status_code}"
        )

        # DO NOT hammer the API on rate limits.
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")

            print(
                "Groq rate limit reached."
            )

            if retry_after:
                print(
                    f"Provider requested retry after "
                    f"{retry_after} seconds."
                )

            print(
                "No retry will be performed during this run."
            )

            return local_fallback_results(jobs)

        response.raise_for_status()

        data = response.json()

        content = data["choices"][0]["message"]["content"]

        results = extract_json(content)

        if not isinstance(results, list):
            raise ValueError(
                "Groq returned JSON, but not a JSON array."
            )

        return merge_ai_results(jobs, results)

    except Exception as error:
        print(
            f"Groq batch matching failed: {error}"
        )

        print(
            "Falling back to local scoring."
        )

        return local_fallback_results(jobs)


def merge_ai_results(
    jobs: list[dict],
    results: list[dict],
) -> list[dict]:
    """
    Attach AI results to the original job objects.
    """

    result_by_index = {}

    for result in results:
        try:
            index = int(result.get("job_index"))
            result_by_index[index] = result
        except (TypeError, ValueError):
            continue

    merged = []

    for index, job in enumerate(jobs, start=1):
        result = result_by_index.get(index)

        if not result:
            print(
                f"Missing AI result for job {index}. "
                f"Using local score."
            )
            result = {
                "match_score": job.get("local_score", 0),
                "qualification": "MODERATE_MATCH",
                "experience_fit": "MODERATE",
                "technical_fit": job.get("local_score", 0),
                "role_fit": job.get("local_score", 0),
                "key_matches": job.get(
                    "local_reasons",
                    [],
                ),
                "missing_requirements": [],
                "concerns": [
                    "AI result unavailable for this job."
                ],
                "reason": (
                    "Ranked using local fallback scoring "
                    "because the batch AI response did not "
                    "contain a result for this job."
                ),
            }

        job["match_score"] = safe_int(
            result.get(
                "match_score",
                job.get("local_score", 0),
            )
        )

        job["qualification"] = result.get(
            "qualification",
            "MODERATE_MATCH",
        )

        job["experience_fit"] = result.get(
            "experience_fit",
            "MODERATE",
        )

        job["technical_fit"] = safe_int(
            result.get("technical_fit", 0)
        )

        job["role_fit"] = safe_int(
            result.get("role_fit", 0)
        )

        job["key_matches"] = result.get(
            "key_matches",
            [],
        )

        job["missing_requirements"] = result.get(
            "missing_requirements",
            [],
        )

        job["concerns"] = result.get(
            "concerns",
            [],
        )

        job["match_reason"] = result.get(
            "reason",
            "",
        )

        merged.append(job)

    return merged


def local_fallback_results(
    jobs: list[dict],
) -> list[dict]:
    """
    Produce usable results without Groq.

    This guarantees that a 429 does not become:
        0 jobs found
    """

    results = []

    for job in jobs:
        score = job.get("local_score", 0)

        if score >= 85:
            qualification = "STRONG_MATCH"
        elif score >= 75:
            qualification = "GOOD_MATCH"
        elif score >= 60:
            qualification = "MODERATE_MATCH"
        else:
            qualification = "WEAK_MATCH"

        job["match_score"] = score
        job["qualification"] = qualification
        job["experience_fit"] = (
            "GOOD"
            if any(
                term in get_job_text(job)
                for term in FRESHER_TERMS
            )
            else "MODERATE"
        )
        job["technical_fit"] = min(
            100,
            score,
        )
        job["role_fit"] = min(
            100,
            score,
        )
        job["key_matches"] = job.get(
            "technical_matches",
            [],
        )
        job["missing_requirements"] = []
        job["concerns"] = [
            "AI batch matcher unavailable; "
            "local scoring used."
        ]
        job["match_reason"] = (
            "This job passed the local AI/ML relevance "
            "filter and was ranked using deterministic "
            "candidate-skill and role matching."
        )

        results.append(job)

    return results


def safe_int(value: Any) -> int:
    """
    Safely convert model output to an integer.
    """

    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0


def score_job(job: dict) -> dict:
    """
    Backward-compatible single-job matcher.

    IMPORTANT:
    This function exists for the old test file.

    Production execution should use score_jobs_batch().
    """

    results = score_jobs_batch([job])

    if not results:
        return local_fallback_results([job])[0]

    return results[0]
