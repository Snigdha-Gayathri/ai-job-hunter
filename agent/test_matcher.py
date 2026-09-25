import json
from datetime import datetime, timezone, timedelta

from job_matcher import (
    local_score_job,
    locally_filter_jobs,
    score_jobs_batch,
)
from main import (
    parse_posted_time,
    calculate_freshness,
    extract_linkedin_job_id,
    get_job_id,
    process_jobs_freshness_and_state,
    score_and_rank_jobs,
    format_ist_and_utc,
    FRESHNESS_WINDOW_MINUTES,
)


print("=" * 70)
print("TEST 1: LOCAL JOB FILTER & AI MATCHER")
print("=" * 70)

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

for job in test_jobs:
    result = local_score_job(job)
    print(f"Job: {job['title']} -> Score: {result['local_score']}/100")
    print("  Reasons:", result["local_reasons"])

candidates = locally_filter_jobs(test_jobs)
assert len(candidates) == 1, f"Expected 1 candidate, got {len(candidates)}"
print(f"Candidates surviving local filter: {len(candidates)}")

scored = score_jobs_batch(candidates)
assert len(scored) == 1
assert scored[0]["match_score"] >= 75
print(f"Scored job: {scored[0]['title']} -> {scored[0]['match_score']}/100")


print()
print("=" * 70)
print("TEST 2: STABLE LINKEDIN JOB ID EXTRACTION")
print("=" * 70)

job_samples = [
    ({"jobId": "4123456789"}, "4123456789"),
    ({"id": "4123456789"}, "4123456789"),
    ({"jobUrl": "https://www.linkedin.com/jobs/view/4123456789/?trackingId=abc"}, "4123456789"),
    ({"url": "https://www.linkedin.com/jobs/search/?currentJobId=4123456789&geoId=102713980"}, "4123456789"),
    ({"title": "AI Engineer", "companyName": "Tech Corp", "location": "Hyderabad"}, "ai engineer|tech corp|hyderabad"),
]

for sample, expected in job_samples:
    extracted = get_job_id(sample)
    print(f"Input: {list(sample.keys())} -> Extracted: {extracted}")
    assert extracted == expected, f"Expected {expected}, got {extracted}"


print()
print("=" * 70)
print("TEST 3: FRESHNESS CALCULATION & LATENCY")
print("=" * 70)

# Reference time: 10:00 AM UTC
ref_time = datetime(2026, 9, 24, 10, 0, 0, tzinfo=timezone.utc)

# 1. Job posted at 09:47 AM UTC (13 minutes ago)
job_fresh = {
    "title": "AI Engineer",
    "postedAt": "2026-09-24T09:47:00Z",
}
fresh_res = calculate_freshness(job_fresh, discovered_at=ref_time)
print("09:47 Job Freshness:", fresh_res)
assert fresh_res["is_fresh"] is True
assert abs(fresh_res["discovery_latency_minutes"] - 13.0) < 0.1
assert "13 minutes" in fresh_res["latency_formatted"]

# 2. Job posted 4 hours ago (06:00 AM UTC)
job_stale = {
    "title": "Stale ML Engineer",
    "postedAt": "2026-09-24T06:00:00Z",
}
stale_res = calculate_freshness(job_stale, discovered_at=ref_time)
print("06:00 Job Freshness:", stale_res)
assert stale_res["is_fresh"] is False
assert stale_res["discovery_latency_minutes"] == 240.0
assert "Stale" in stale_res["freshness_reason"]

# 3. Relative text timestamp '14 minutes ago'
job_relative = {
    "title": "GenAI Engineer",
    "postedAtText": "14 minutes ago",
}
rel_res = calculate_freshness(job_relative, discovered_at=ref_time)
print("Relative 14m Freshness:", rel_res)
assert rel_res["is_fresh"] is True
assert abs(rel_res["discovery_latency_minutes"] - 14.0) < 0.1

# 4. Unparseable timestamp
job_unknown = {
    "title": "Unknown Date Job",
    "postedAt": "some invalid date format",
}
unk_res = calculate_freshness(job_unknown, discovered_at=ref_time)
print("Unknown Date Freshness:", unk_res)
assert unk_res["is_fresh"] is False
assert "cannot verify" in unk_res["freshness_reason"]


print()
print("=" * 70)
print("TEST 4: THE 09:47 AM -> 10:00 AM ACCEPTANCE SCENARIO")
print("=" * 70)

# Simulate State
simulated_state = {"jobs": {}, "last_run": {}}

# Job posted at 09:47 AM IST (04:17 UTC)
posted_time = datetime(2026, 9, 24, 4, 17, 0, tzinfo=timezone.utc)
discovered_time_10am = datetime(2026, 9, 24, 4, 30, 0, tzinfo=timezone.utc)

job_947 = {
    "jobId": "9470001",
    "title": "Junior AI Engineer - LLM & Agentic AI",
    "companyName": "NextGen AI Lab",
    "location": "Hyderabad, India",
    "postedAt": posted_time.isoformat(),
    "jobUrl": "https://www.linkedin.com/jobs/view/9470001",
    "descriptionHtml": """
    We are hiring a Junior AI Engineer for our Agentic AI team.
    Tech: Python, PyTorch, LangChain, LangGraph, RAG, FastAPI.
    0-1 years experience or 2026 freshers welcome.
    """,
}

run_metadata_10am = {
    "jobs_retrieved": 1,
    "jobs_within_freshness_window": 0,
    "stale_jobs_filtered": 0,
    "duplicates_skipped": 0,
    "email_retries": 0,
    "new_fresh_jobs": 0,
    "eligible_jobs": 0,
    "candidates_surviving_filter": 0,
    "high_match_jobs": 0,
    "emails_sent": 0,
    "scraper_errors": [],
    "workflow_errors": [],
}

# --- 10:00 AM RUN ---
print(f"Simulating 10:00 AM Scan at {format_ist_and_utc(discovered_time_10am)}...")
eligible_10am = process_jobs_freshness_and_state(
    raw_jobs=[job_947],
    state=simulated_state,
    discovered_at=discovered_time_10am,
    run_metadata=run_metadata_10am,
)

assert len(eligible_10am) == 1, "09:47 job should be eligible at 10:00 AM"
assert eligible_10am[0]["_freshness"]["is_fresh"] is True
assert abs(eligible_10am[0]["_discovery_latency_minutes"] - 13.0) < 0.1
print(f"Latency: {eligible_10am[0]['_latency_formatted']} (Verified ~13 mins)")

# Score the job
matched_10am = score_and_rank_jobs(eligible_10am, run_metadata_10am)
assert len(matched_10am) == 1
assert matched_10am[0]["match_score"] >= 75
print(f"Matched Job Score: {matched_10am[0]['match_score']}/100")

# Simulate successful email sending
simulated_state["jobs"]["9470001"]["status"] = "emailed"
simulated_state["jobs"]["9470001"]["emailed_at"] = discovered_time_10am.isoformat()
print("10:00 AM Run successfully emailed the job!")

# --- 11:00 AM SUBSEQUENT RUN ---
print()
discovered_time_11am = datetime(2026, 9, 24, 5, 30, 0, tzinfo=timezone.utc)
print(f"Simulating 11:00 AM Subsequent Scan at {format_ist_and_utc(discovered_time_11am)}...")

run_metadata_11am = {
    "jobs_retrieved": 1,
    "jobs_within_freshness_window": 0,
    "stale_jobs_filtered": 0,
    "duplicates_skipped": 0,
    "email_retries": 0,
    "new_fresh_jobs": 0,
    "eligible_jobs": 0,
    "candidates_surviving_filter": 0,
    "high_match_jobs": 0,
    "emails_sent": 0,
    "scraper_errors": [],
    "workflow_errors": [],
}

eligible_11am = process_jobs_freshness_and_state(
    raw_jobs=[job_947],
    state=simulated_state,
    discovered_at=discovered_time_11am,
    run_metadata=run_metadata_11am,
)

assert len(eligible_11am) == 0, "Job must NOT be re-emailed in subsequent run!"
assert run_metadata_11am["duplicates_skipped"] == 1
print("11:00 AM Scan correctly suppressed previously emailed job via deduplication.")


print()
print("=" * 70)
print("TEST 5: FAILSAFE RETRY UPON EMAIL FAILURE")
print("=" * 70)

# Simulate a job that was discovered, but email sending failed
retry_state = {"jobs": {}, "last_run": {}}
job_retry = {
    "jobId": "8880001",
    "title": "AI Engineer",
    "companyName": "AI Startup",
    "location": "Bengaluru, India",
    "postedAt": (discovered_time_10am - timedelta(minutes=10)).isoformat(),
    "descriptionHtml": "Python, LLM, RAG, PyTorch.",
}

# Run 1: discovered, but email failed
run_meta_fail = {
    "jobs_retrieved": 1, "jobs_within_freshness_window": 0,
    "stale_jobs_filtered": 0, "duplicates_skipped": 0, "email_retries": 0,
    "new_fresh_jobs": 0, "eligible_jobs": 0, "candidates_surviving_filter": 0,
    "high_match_jobs": 0, "emails_sent": 0, "scraper_errors": [], "workflow_errors": []
}
el_1 = process_jobs_freshness_and_state([job_retry], retry_state, discovered_time_10am, run_meta_fail)
assert len(el_1) == 1

# Mark as failed email
retry_state["jobs"]["8880001"]["status"] = "email_failed"
retry_state["jobs"]["8880001"]["last_error"] = "SMTPConnectError"

# Run 2: Next hour run (within 90 min window)
run_meta_retry = {
    "jobs_retrieved": 1, "jobs_within_freshness_window": 0,
    "stale_jobs_filtered": 0, "duplicates_skipped": 0, "email_retries": 0,
    "new_fresh_jobs": 0, "eligible_jobs": 0, "candidates_surviving_filter": 0,
    "high_match_jobs": 0, "emails_sent": 0, "scraper_errors": [], "workflow_errors": []
}
el_2 = process_jobs_freshness_and_state([job_retry], retry_state, discovered_time_10am + timedelta(minutes=45), run_meta_retry)
assert len(el_2) == 1, "Failed email job must be RETRIED!"
assert run_meta_retry["email_retries"] == 1
print("Verified: Failed email attempt was successfully kept eligible for retry!")


print()
print("=" * 70)
print("TEST 6: FAILURE CASE TRACEABILITY")
print("=" * 70)

# Simulate failure points to verify diagnostic logs pinpoint where a job was filtered
job_rejected_profile = {
    "jobId": "9990001",
    "title": "Lead Senior Director of Java Architecture",
    "companyName": "Enterprise Corp",
    "location": "Mumbai, India",
    "postedAt": discovered_time_10am.isoformat(),
    "descriptionHtml": "10+ years Java Spring enterprise architecture.",
}

fail_trace_state = {"jobs": {}, "last_run": {}}
fail_trace_meta = {
    "jobs_retrieved": 1, "jobs_within_freshness_window": 0,
    "stale_jobs_filtered": 0, "duplicates_skipped": 0, "email_retries": 0,
    "new_fresh_jobs": 0, "eligible_jobs": 0, "candidates_surviving_filter": 0,
    "high_match_jobs": 0, "emails_sent": 0, "scraper_errors": [], "workflow_errors": []
}

el_fail = process_jobs_freshness_and_state([job_rejected_profile], fail_trace_state, discovered_time_10am, fail_trace_meta)
assert len(el_fail) == 1  # fresh timestamp passes to matcher

matched_fail = score_and_rank_jobs(el_fail, fail_trace_meta)
assert len(matched_fail) == 0, "Senior non-AI job must be rejected by matcher!"
print("Verified: Failure diagnosis correctly identified matching rejection for non-AI senior role.")

print()
print("=" * 70)
print("ALL TESTS PASSED SUCCESSFULLY!")
print("=" * 70)
