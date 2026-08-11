import os
import json
import requests


GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]

MODEL_URL = "https://models.github.ai/inference/chat/completions"

MODEL = "openai/gpt-4.1"


CANDIDATE_PROFILE = """
Candidate: Snigdha Gayathri

Education:
- B.Tech in Computer Science and Engineering, AI & ML
- Graduation year: 2026
- CGPA: 8.30

Target:
- Entry-level / fresher AI/ML Engineering roles
- Junior AI Engineer
- AI Engineer
- Machine Learning Engineer
- Generative AI Engineer
- LLM Engineer
- Agentic AI Engineer
- Applied AI Engineer
- AI/ML internships and new-grad roles

Core programming:
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
- prompt engineering
- tool calling
- structured outputs
- multi-agent orchestration
- embeddings
- hybrid retrieval
- vector search

Databases / Retrieval:
- Qdrant
- Pinecone
- Supabase
- Neo4j

Backend / Software:
- FastAPI
- React
- Next.js
- TypeScript
- Docker

Relevant project experience:
- Agentic Placement RAG
- Enterprise Knowledge Intelligence Platform (EKIP)
- LLM Inference Optimization Lab
- Deep Learning Performance Profiler
- Smart Shelf AI
- multiple AI applications involving RAG, agents and LLMs

Additional technical exposure:
- LLM inference optimization
- quantization
- FP32 / FP16 / BF16
- 4-bit / 8-bit quantization
- KV caching
- transformer architecture
- profiling and benchmarking
- GPU performance analysis
- MLOps concepts
"""


def extract_json(text):
    """
    Extract JSON even if the model wraps it in markdown.
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
            f"Model did not return valid JSON: {text}"
        )

    return json.loads(text[start:end + 1])


def score_job(job):
    title = job.get("title", "")
    company = job.get("companyName", "")
    location = job.get("location", "")

    description = (
        job.get("descriptionHtml")
        or job.get("description")
        or ""
    )

    prompt = f"""
You are an expert technical recruiter and resume-to-job matching system.

Evaluate this job against the candidate profile.

CANDIDATE PROFILE:
{CANDIDATE_PROFILE}

JOB:
Title: {title}
Company: {company}
Location: {location}

Job Description:
{description}

IMPORTANT RULES:

1. This candidate is targeting entry-level/fresher roles.

2. Strongly penalize jobs requiring:
   - 2+ years experience
   - 3+ years experience
   - seniority
   - lead roles
   - staff roles
   - principal roles
   - manager roles
   - architect roles

3. Do not reject a job merely because a preferred skill is missing.

4. Give strong positive weight to:
   - Python
   - LLMs
   - RAG
   - AI agents
   - LangChain
   - LangGraph
   - FastAPI
   - Hugging Face
   - PyTorch
   - vector databases
   - AI inference
   - MLOps
   - machine learning

5. Evaluate actual technical overlap, not keyword overlap.

6. A role can score highly even if the exact job title differs.

7. A role requiring significant unrelated software development should score lower.

8. Return ONLY valid JSON.

Return this exact structure:

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

Scoring:

90-100 = exceptional match
80-89 = strong match
75-79 = good match
60-74 = moderate match
below 60 = weak match

qualification must be one of:

STRONG_MATCH
GOOD_MATCH
MODERATE_MATCH
WEAK_MATCH
"""


   response = requests.post(
    MODEL_URL,
    headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10"
    },
        json={
            "model": MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a precise technical recruiting "
                        "and job matching engine."
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "temperature": 0.1,
            "max_tokens": 800
        },
        timeout=120
    )

    response.raise_for_status()

    data = response.json()

    content = data["choices"][0]["message"]["content"]

    return extract_json(content)
