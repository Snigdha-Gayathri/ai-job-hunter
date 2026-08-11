import json

from job_matcher import score_job


test_job = {
    "title": "AI Engineer - LLM & RAG",
    "companyName": "Test AI Company",
    "location": "Hyderabad, India",
    "descriptionHtml": """
    We are looking for an AI Engineer to build production AI systems.

    Responsibilities:
    - Build LLM applications
    - Develop RAG pipelines
    - Build AI agents
    - Work with Python and FastAPI
    - Use vector databases
    - Build and deploy ML inference services

    Requirements:
    - Python
    - Machine Learning
    - LLMs
    - RAG
    - AI agents
    - FastAPI

    Fresh graduates with strong project experience are encouraged
    to apply.
    """
}


print("=" * 70)
print("TESTING AI JOB MATCHER")
print("=" * 70)

result = score_job(test_job)

print()
print(json.dumps(result, indent=2))

print()
print("=" * 70)
print("MATCHER TEST COMPLETE")
print("=" * 70)
