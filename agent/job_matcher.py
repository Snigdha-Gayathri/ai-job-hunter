import os
import json
import requests


GROQ_API_KEY = os.environ["GROQ_API_KEY"]

MODEL_URL = "https://api.groq.com/openai/v1/chat/completions"

MODEL = "openai/gpt-oss-20b"


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


def extract_json(text):
    """
    Extract JSON from the model response.
    Handles responses wrapped in markdown code fences.
    """

    text = text.strip()

    if text.startswith("```"):
        text = text.replace("```json", "")
        text = text.replace("```", "")
        text = text.strip()

    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1:
        raise ValueError(
            f"Model did not return valid JSON:\n{text}"
        )

    return json.loads(
        text[start:end + 1]
    )


def score_job(job):

    title = job.get(
        "title",
        ""
    )

    company = job.get(
        "companyName",
        ""
    )

    location = job.get(
        "location",
        ""
    )

    description = (
        job.get("description")
        or job.get("descriptionHtml")
        or ""
    )

    # Prevent excessively large job descriptions
    # from unnecessarily increasing the prompt size.
    description = str(description)[:15000]

    prompt = f"""
You are an expert technical recruiter.

Your task is to determine how strongly this job matches
the candidate described below.


# CANDIDATE PROFILE

{CANDIDATE_PROFILE}


# JOB

Title:
{title}

Company:
{company}

Location:
{location}

Description:
{description}


# IMPORTANT EVALUATION RULES

1. The candidate is a 2026 graduate targeting entry-level
   and junior AI/ML engineering roles.

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

4. Treat "0-2 years" or "0-1 years" as compatible.

5. Do NOT reject a role simply because the candidate does
   not have every preferred technology.

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

9. Distinguish between:

   - required qualifications
   - preferred qualifications
   - responsibilities

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
    the score accordingly.

16. If the job explicitly requires more experience than a
    fresh graduate can reasonably satisfy, reflect that
    heavily in the score.

17. Evaluate the actual responsibilities, not just the title.

18. A strong project match is valuable evidence, but projects
    must not be presented as professional employment.


# SCORING

90-100:
Exceptional match. Candidate should strongly consider applying.

80-89:
Strong match. Candidate is highly relevant.

75-79:
Good match. Candidate should consider applying.

60-74:
Moderate match. Some meaningful overlap but significant gaps.

Below 60:
Weak match.


RETURN ONLY VALID JSON.

Use exactly this structure:

{{
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
"""

    print(
        f"Scoring: {title} | {company}"
    )

    response = requests.post(
        MODEL_URL,
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a precise technical recruiting "
                        "and job matching engine. "
                        "Return only valid JSON when requested."
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "temperature": 0.1,
            "max_tokens": 1000
        },
        timeout=120
    )

    if response.status_code != 200:
        print("Groq API response:")
        print(response.text)

    response.raise_for_status()

    data = response.json()

    content = data["choices"][0]["message"]["content"]

    return extract_json(content)
