import json

from job_matcher import (
    local_score_job,
    locally_filter_jobs,
    score_jobs_batch,
)


test_jobs = [
    {
        "title": "AI Engineer - LLM & RAG",
        "companyName": "Test AI Company",
        "location": "Hyderabad, India",
        "descriptionHtml": """
        We are looking for an AI Engineer to build
        production AI systems.

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

        Fresh graduates with strong project experience
        are encouraged to apply.
        """,
    },
    {
        "title": "Senior Java Backend Engineer",
        "companyName": "Unrelated Company",
        "location": "Bangalore, India",
        "descriptionHtml": """
        We are looking for a Senior Java Backend Engineer
        with 6+ years of experience building enterprise
        backend systems.
        """,
    },
]


print("=" * 70)
print("TESTING LOCAL JOB FILTER")
print("=" * 70)

for job in test_jobs:
    result = local_score_job(job)

    print()
    print(
        f"Job: {job['title']}"
    )

    print(
        f"Local score: "
        f"{result['local_score']}/100"
    )

    print(
        "Reasons:",
        result["local_reasons"],
    )


print()
print("=" * 70)
print("TESTING LOCAL CANDIDATE FILTER")
print("=" * 70)

candidates = locally_filter_jobs(
    test_jobs
)

print(
    f"Candidates surviving local filter: "
    f"{len(candidates)}"
)

for job in candidates:
    print(
        f"- {job['title']} "
        f"({job['local_score']}/100)"
    )


print()
print("=" * 70)
print("TESTING GROQ BATCH MATCHER")
print("=" * 70)

if candidates:
    results = score_jobs_batch(
        candidates
    )

    print()
    print(
        json.dumps(
            results,
            indent=2,
        )
    )

else:
    print(
        "No candidates survived local filtering."
    )


print()
print("=" * 70)
print("MATCHER TEST COMPLETE")
print("=" * 70)
