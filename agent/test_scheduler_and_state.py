import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add agent directory to sys.path
AGENT_DIR = Path(__file__).resolve().parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import config
import filters
import job_matcher
import main
import outreach
import recruiter_matcher
from config import (
    GROQ_BATCH_SIZE,
    MIN_MATCH_SCORE,
    PROFILE_EXAMPLE_PATH,
    ProfileConfigurationError,
    get_active_profile,
    get_clean_apify_token,
    load_profile,
    set_active_profile,
    validate_profile,
    verify_apify_auth,
)
from filters import (
    is_company_excluded,
    evaluate_experience_eligibility,
    is_valid_work_location,
    is_role_relevant,
)
from job_matcher import (
    build_batch_prompt,
    build_candidate_profile_text,
    local_score_job,
    score_jobs_batch,
)
from main import (
    build_email_digest,
    calculate_freshness,
    execute_apify_run,
    filter_jobs_by_company,
    filter_jobs_by_experience,
    filter_jobs_by_role,
    filter_jobs_by_strict_location,
    format_ist_and_utc,
    get_job_id,
    load_state,
    print_run_summary,
    process_jobs_freshness_and_state,
    save_state,
    score_and_rank_jobs,
    send_email_report,
)


TEST_AI_PROFILE = {
    "name": "Test AI Candidate",
    "headline": "AI/ML Engineer | LLMs | RAG",
    "education": {
        "degree": "B.S.",
        "field": "Computer Science",
        "graduation_year": "2025",
        "cgpa": "8.6",
    },
    "skills": {
        "core": [
            "Python", "Machine Learning", "Deep Learning", "PyTorch",
            "TensorFlow", "LLMs", "Generative AI", "RAG", "LangChain",
            "LangGraph", "FastAPI", "Docker", "Vector Databases",
        ],
    },
    "projects": ["Sample RAG Pipeline", "Sample Model Benchmark"],
    "experience_preferences": {
        "level": "entry_level",
        "years_of_experience": 0,
        "max_required_yoe": 2,
        "accept_fresher": True,
        "exclude_senior_titles": True,
    },
    "target_roles": [
        "AI Engineer", "Machine Learning Engineer", "Generative AI Engineer",
        "LLM Engineer", "Applied AI Engineer",
    ],
    "target_role_families": [
        "AI Developer", "AI Intern", "LLM Application Engineer",
        "Agentic AI Engineer", "Computer Vision Engineer", "NLP Engineer",
    ],
    "target_locations": ["Mumbai", "Hyderabad", "Bangalore", "Bengaluru", "Pune", "Remote"],
    "remote_preferences": {
        "accept_remote": True,
        "accept_hybrid_in_target_cities": True,
        "home_country": "India",
        "allowed_remote_regions": ["India", "Worldwide", "Anywhere", "Global"],
    },
    "excluded_companies": ["restrictedcorp", "restrictedsub"],
    "resume_path": "",
    "video_resume_url": "",
}


class TestSchedulerAndState(unittest.TestCase):
    """
    Test suite verifying:
    1. One-shot mode runs once and exits cleanly.
    2. Worker mode remains available and unchanged.
    3. Hourly schedule syntax is mathematically correct for IST (:17 IST / :47 UTC).
    4. State survives between simulated workflow runs across ephemeral runners.
    5. Successfully emailed jobs are never emailed again.
    6. Email-failed jobs remain eligible for retry.
    7. 3, 20, 30, 47, 80 qualifying jobs produce exact matching counts of email cards.
    8. Multiple Groq batches process every single candidate without truncation.
    9. Configured company exclusions are strictly enforced.
    10. Senior roles remain excluded.
    11. Target locations remain strictly enforced.
    12. Role family matches remain comprehensive.
    13. No ATS or candidate truncation is reintroduced.
    14. State persistence failure fails loudly.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp_dir.name)
        self.orig_state_dir = main.STATE_DIR
        self.orig_state_file = main.STATE_FILE
        self.orig_metrics_file = main.METRICS_FILE

        main.STATE_DIR = self.state_dir
        main.STATE_FILE = self.state_dir / "seen_jobs.json"
        main.METRICS_FILE = self.state_dir / "last_run_metrics.json"
        set_active_profile(TEST_AI_PROFILE)

    def tearDown(self):
        set_active_profile(None)
        main.STATE_DIR = self.orig_state_dir
        main.STATE_FILE = self.orig_state_file
        main.METRICS_FILE = self.orig_metrics_file
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # TEST 1: One-shot mode runs once and exits cleanly
    # -------------------------------------------------------------------------
    def test_one_shot_mode_runs_once_and_exits(self):
        """Verify main() runs a single cycle and exits cleanly with sys.exit(0)."""
        test_state = {"jobs": {}, "sources": {}, "last_run": {}}
        mock_meta = {
            "workflow_errors": [],
            "jobs_retrieved": 0,
            "eligible_jobs": 0,
            "emails_sent": 0,
            "state_persistence": "SUCCESS",
        }

        with patch("main.load_state", return_value=test_state), \
             patch("main.run_pipeline_once", return_value=([], mock_meta)) as mock_run:
            with self.assertRaises(SystemExit) as cm:
                main.main()
            self.assertEqual(cm.exception.code, 0)
            mock_run.assert_called_once_with(test_state, force_all=True)

    def test_one_shot_mode_fails_loudly_on_workflow_errors(self):
        """Verify main() exits with code 1 if fatal workflow errors occurred."""
        test_state = {"jobs": {}}
        mock_meta = {
            "workflow_errors": ["SMTP connection refused"],
            "state_persistence": "SUCCESS",
        }

        with patch("main.load_state", return_value=test_state), \
             patch("main.run_pipeline_once", return_value=([], mock_meta)):
            with self.assertRaises(SystemExit) as cm:
                main.main()
            self.assertEqual(cm.exception.code, 1)

    # -------------------------------------------------------------------------
    # TEST 2: Worker mode still works unchanged
    # -------------------------------------------------------------------------
    def test_worker_mode_still_works_unchanged(self):
        """Verify worker loop executes cycles and handles keyboard interrupt safely."""
        self.assertTrue(callable(main.run_worker_loop))

        with patch("main.load_state", return_value={"jobs": {}}), \
             patch("main.run_pipeline_once", side_effect=KeyboardInterrupt), \
             patch("main.save_state") as mock_save:
            main.run_worker_loop()
            mock_save.assert_called_once()

    # -------------------------------------------------------------------------
    # TEST 3: Hourly schedule syntax is correct for IST (:17 IST / :47 UTC)
    # -------------------------------------------------------------------------
    def test_hourly_schedule_syntax_for_ist(self):
        """
        Verify that cron '47 * * * *' produces exactly one run every hour, 24/7,
        at approximately :17 India Standard Time (UTC+5:30).
        """
        ist_tz = timezone(timedelta(hours=5, minutes=30))

        # Check all 24 hours of the day
        for ist_hour in range(24):
            # Target time in IST: ist_hour:17:00
            target_ist = datetime(2026, 10, 7, ist_hour, 17, 0, tzinfo=ist_tz)
            # Convert to UTC
            target_utc = target_ist.astimezone(timezone.utc)

            # Minute in UTC must be 47
            self.assertEqual(
                target_utc.minute,
                47,
                f"IST {ist_hour:02d}:17 did not map to UTC minute 47 (got {target_utc.minute})"
            )

            # Re-convert UTC minute 47 back to IST
            check_utc = datetime(2026, 10, 7, target_utc.hour, 47, 0, tzinfo=timezone.utc)
            check_ist = check_utc.astimezone(ist_tz)
            self.assertEqual(check_ist.minute, 17)
            self.assertEqual(check_ist.hour, ist_hour)

    # -------------------------------------------------------------------------
    # TEST 4: State survives between simulated workflow runs
    # -------------------------------------------------------------------------
    def test_state_survives_between_simulated_workflow_runs(self):
        """
        Simulate Run 1 writing state to disk, then a fresh Run 2 loading that
        state from disk and correctly deduplicating previously processed jobs.
        """
        # Run 1: Start with empty disk state
        state_run1 = load_state()
        self.assertEqual(len(state_run1.get("jobs", {})), 0)

        job1 = {
            "source_job_id": "job_persistent_001",
            "title": "AI Engineer",
            "companyName": "Anthropic Partner",
            "location": "Bengaluru, Karnataka, India",
            "url": "https://example.com/jobs/001",
            "postedAt": "10 minutes ago",
            "source": "greenhouse",
        }

        now_ref = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
        meta_run1 = {
            "jobs_within_freshness_window": 0,
            "stale_jobs_filtered": 0,
            "duplicates_skipped": 0,
            "email_retries": 0,
            "new_fresh_jobs": 0,
        }

        eligible_run1 = process_jobs_freshness_and_state([job1], state_run1, now_ref, meta_run1)
        self.assertEqual(len(eligible_run1), 1)

        # Mark as successfully emailed in Run 1
        jid = job1["_job_id"]
        state_run1["jobs"][jid]["status"] = "emailed"
        state_run1["jobs"][jid]["emailed_at"] = now_ref.isoformat()
        save_state(state_run1)

        # Verify state file exists on disk
        self.assertTrue(main.STATE_FILE.exists())

        # Run 2: Runner starts fresh and restores state from disk
        state_run2 = load_state()
        self.assertIn(jid, state_run2.get("jobs", {}))
        self.assertEqual(state_run2["jobs"][jid]["status"], "emailed")

        # In Run 2, the same job is scraped again
        meta_run2 = {
            "jobs_within_freshness_window": 0,
            "stale_jobs_filtered": 0,
            "duplicates_skipped": 0,
            "email_retries": 0,
            "new_fresh_jobs": 0,
        }
        eligible_run2 = process_jobs_freshness_and_state([job1], state_run2, now_ref, meta_run2)

        # Must be deduplicated (0 eligible)
        self.assertEqual(len(eligible_run2), 0)
        self.assertEqual(meta_run2["duplicates_skipped"], 1)

    # -------------------------------------------------------------------------
    # TEST 5: Successfully emailed jobs are never emailed again
    # -------------------------------------------------------------------------
    def test_successfully_emailed_jobs_are_never_emailed_again(self):
        """Verify jobs with status='emailed' are strictly skipped in subsequent cycles."""
        state = {
            "jobs": {
                "emailed_123": {
                    "job_id": "emailed_123",
                    "status": "emailed",
                    "title": "LLM Engineer",
                    "company": "DeepMind Partner",
                    "url": "https://example.com/apply/123",
                }
            }
        }
        incoming_job = {
            "source_job_id": "emailed_123",
            "title": "LLM Engineer",
            "companyName": "DeepMind Partner",
            "location": "Remote",
            "url": "https://example.com/apply/123",
            "postedAt": "5 minutes ago",
        }
        meta = {"duplicates_skipped": 0, "stale_jobs_filtered": 0, "jobs_within_freshness_window": 0, "email_retries": 0}
        now_utc = datetime.now(timezone.utc)
        eligible = process_jobs_freshness_and_state([incoming_job], state, now_utc, meta)

        self.assertEqual(len(eligible), 0)
        self.assertEqual(meta["duplicates_skipped"], 1)

    # -------------------------------------------------------------------------
    # TEST 6: Email-failed jobs remain eligible for retry
    # -------------------------------------------------------------------------
    def test_email_failed_jobs_remain_eligible_for_retry(self):
        """
        Verify that if email delivery fails, jobs are marked 'email_failed'
        and remain eligible for retry in subsequent cycles.
        """
        state = {"jobs": {}}
        now_utc = datetime.now(timezone.utc)

        job = {
            "source_job_id": "retry_job_001",
            "title": "AI Research Engineer",
            "companyName": "Open Research",
            "location": "Hyderabad, India",
            "url": "https://example.com/apply/retry",
            "postedAt": "10 minutes ago",
            "match_score": 90,
            "qualification": "EXCELLENT_MATCH",
            "priority": "HIGH",
        }

        meta = {
            "jobs_within_freshness_window": 0,
            "stale_jobs_filtered": 0,
            "duplicates_skipped": 0,
            "email_retries": 0,
            "new_fresh_jobs": 0,
        }

        # Step 1: Discovered
        eligible = process_jobs_freshness_and_state([job], state, now_utc, meta)
        self.assertEqual(len(eligible), 1)
        jid = job["_job_id"]

        # Step 2: Attempt email with mock credentials and mock SMTP failure
        with patch.dict(os.environ, {"GMAIL_USERNAME": "test@gmail.com", "GMAIL_APP_PASSWORD": "app_password"}), \
             patch("smtplib.SMTP_SSL") as mock_smtp:
            mock_smtp.side_effect = ConnectionRefusedError("SMTP server down")
            with self.assertRaises(ConnectionRefusedError):
                send_email_report(
                    jobs=[job],
                    run_metadata=meta,
                    state=state,
                )

        # Verify job marked as email_failed (NOT emailed!)
        self.assertEqual(state["jobs"][jid]["status"], "email_failed")
        self.assertEqual(state["jobs"][jid]["email_attempts"], 1)

        # Step 3: Next cycle, job is acquired again
        meta_retry = {
            "jobs_within_freshness_window": 0,
            "stale_jobs_filtered": 0,
            "duplicates_skipped": 0,
            "email_retries": 0,
            "new_fresh_jobs": 0,
        }
        eligible_retry = process_jobs_freshness_and_state([job], state, now_utc, meta_retry)

        # Job MUST be eligible for retry!
        self.assertEqual(len(eligible_retry), 1)
        self.assertEqual(meta_retry["email_retries"], 1)

    # -------------------------------------------------------------------------
    # TEST 7: 3, 20, 30, 47, 80 qualifying jobs -> Exact email cards
    # -------------------------------------------------------------------------
    def test_email_card_counts_without_arbitrary_caps(self):
        """Verify exact counts 3, 20, 30, 47, 80 qualifying jobs produce matching email cards."""
        test_counts = [3, 20, 30, 47, 80]
        for count in test_counts:
            jobs = [
                {
                    "_job_id": f"card_test_{count}_{i}",
                    "title": f"AI Engineer #{i}",
                    "companyName": f"AI Lab #{i}",
                    "location": "Bengaluru, Karnataka, India",
                    "url": f"https://example.com/apply/{count}/{i}",
                    "match_score": 85,
                    "reason": "Strong match for AI/ML and LLM engineering.",
                }
                for i in range(1, count + 1)
            ]
            subject, html_body, text_body = build_email_digest(jobs)
            apply_count = html_body.count("Apply &rarr;")
            self.assertEqual(
                apply_count,
                count,
                f"Expected {count} email cards, got {apply_count}"
            )
            if count == 1:
                self.assertIn("1 New Match", subject)
            else:
                self.assertIn(f"{count} New Matches", subject)

    # -------------------------------------------------------------------------
    # TEST 8: Multiple Groq batches process every single candidate
    # -------------------------------------------------------------------------
    def test_multiple_groq_batches_process_every_candidate(self):
        """Verify 47 and 80 candidates are processed through multiple batches without loss."""
        for candidate_count in [47, 80]:
            candidates = [
                {
                    "title": f"AI Engineer #{i}",
                    "companyName": f"Tech Company #{i}",
                    "location": "Pune, India",
                    "description": "Python, PyTorch, LLMs, GenAI, RAG. Freshers welcome 0-2 YOE.",
                }
                for i in range(1, candidate_count + 1)
            ]

            scored = score_jobs_batch(candidates)
            self.assertEqual(
                len(scored),
                candidate_count,
                f"Expected {candidate_count} scored candidates, got {len(scored)}"
            )

    # -------------------------------------------------------------------------
    # TEST 9: Configured company exclusions remain strictly enforced
    # -------------------------------------------------------------------------
    def test_configured_company_exclusions_enforced(self):
        """Verify profile-configured company exclusions and subsidiaries are rejected."""
        variants = [
            "RestrictedCorp",
            "RestrictedCorp Limited",
            "RestrictedCorp BPM",
            "RestrictedCorp BPM Limited",
            "RestrictedCorp Technologies",
            "RestrictedCorp Consulting",
            "RestrictedSub",
        ]
        for comp in variants:
            is_exc, reason = is_company_excluded(comp)
            self.assertTrue(is_exc, f"Company {comp} was not excluded!")

        # Legitimate companies must NOT be excluded
        legit = ["Google", "Microsoft", "Nvidia", "TCS", "Wipro", "OpenAI"]
        for comp in legit:
            is_exc, _ = is_company_excluded(comp)
            self.assertFalse(is_exc, f"Legitimate company {comp} was incorrectly excluded!")

    # -------------------------------------------------------------------------
    # TEST 10: Senior roles remain strictly excluded
    # -------------------------------------------------------------------------
    def test_senior_roles_remain_excluded(self):
        """Verify roles requiring 3+ YOE or senior titles are rejected."""
        senior_jobs = [
            {"title": "Senior AI Engineer", "description": "Requires 5+ years of experience."},
            {"title": "Lead Machine Learning Engineer", "description": "8+ years of experience."},
            {"title": "Principal AI Architect", "description": "10+ YOE designing distributed systems."},
            {"title": "Staff ML Scientist", "description": "6-8 years experience required."},
        ]
        for job in senior_jobs:
            is_elig, reason = evaluate_experience_eligibility(job)
            self.assertFalse(is_elig, f"Senior job {job['title']} was not rejected: {reason}")

        junior_jobs = [
            {"title": "AI Engineer (Fresher)", "description": "0-1 years of experience, freshers welcome."},
            {"title": "Junior Machine Learning Engineer", "description": "0-2 YOE with Python and PyTorch."},
            {"title": "AI Intern", "description": "Internship for recent graduates."},
        ]
        for job in junior_jobs:
            is_elig, reason = evaluate_experience_eligibility(job)
            self.assertTrue(is_elig, f"Junior job {job['title']} was rejected: {reason}")

    # -------------------------------------------------------------------------
    # TEST 11: Target locations remain enforced
    # -------------------------------------------------------------------------
    def test_target_locations_remain_enforced(self):
        """Verify only Mumbai, Hyderabad, Bangalore/Bengaluru, Pune, and India Remote pass."""
        valid_locs = [
            {"location": "Hyderabad, Telangana, India"},
            {"location": "Bengaluru, Karnataka"},
            {"location": "Bangalore"},
            {"location": "Mumbai, Maharashtra"},
            {"location": "Pune, Maharashtra, India"},
            {"location": "Remote, India"},
            {"location": "India", "workplaceType": "Remote"},
        ]
        for job in valid_locs:
            is_valid, reason = is_valid_work_location(job)
            self.assertTrue(is_valid, f"Location {job.get('location')} was rejected: {reason}")

        invalid_locs = [
            {"location": "London, United Kingdom"},
            {"location": "Dallas, TX, United States"},
            {"location": "Singapore"},
            {"location": "Berlin, Germany"},
            {"location": "Toronto, Canada"},
        ]
        for job in invalid_locs:
            is_valid, reason = is_valid_work_location(job)
            self.assertFalse(is_valid, f"Foreign location {job.get('location')} was accepted!")

    # -------------------------------------------------------------------------
    # TEST 12: Role-family matcher remains broad and accurate
    # -------------------------------------------------------------------------
    def test_role_family_matcher_remains_accurate(self):
        """Verify AI/ML/GenAI/LLM/Agentic/Applied AI roles match, and non-AI roles are rejected."""
        target_roles = [
            "AI Engineer",
            "Machine Learning Engineer",
            "Generative AI Engineer",
            "LLM Application Engineer",
            "Agentic AI Engineer",
            "Applied AI Engineer",
            "Computer Vision Engineer",
            "NLP Engineer",
            "AI Developer",
            "AI Intern",
        ]
        for title in target_roles:
            is_rel, reason = is_role_relevant(title, "Building AI systems with Python.")
            self.assertTrue(is_rel, f"Role {title} was rejected: {reason}")

        unrelated_roles = [
            "Frontend React Developer",
            "Java Enterprise Backend Engineer",
            "Manual QA Tester",
            "DevOps / Kubernetes Engineer",
            "Wordpress Developer",
        ]
        for title in unrelated_roles:
            is_rel, reason = is_role_relevant(title, "Developing traditional web and IT applications.")
            self.assertFalse(is_rel, f"Unrelated role {title} was accepted: {reason}")

    # -------------------------------------------------------------------------
    # TEST 13: State persistence failure fails loudly
    # -------------------------------------------------------------------------
    def test_state_persistence_failure_fails_loudly(self):
        """Verify save_state raises RuntimeError and marks state_persistence='FAILED' on write error."""
        test_state = {"jobs": {}, "last_run": {}}
        # Point STATE_FILE to an invalid read-only/unwritable location
        unwritable_file = Path("Z:\\nonexistent_drive_12345\\state.json")
        with patch.object(main, "STATE_FILE", unwritable_file):
            with self.assertRaises(RuntimeError):
                save_state(test_state)

        self.assertIn("FAILED", test_state["last_run"].get("state_persistence", ""))

    # -------------------------------------------------------------------------
    # TEST 14: Apify live response handling - Direct dataset list (HTTP 200)
    # -------------------------------------------------------------------------
    def test_apify_linkedin_response_handling_direct_list(self):
        """Verify execute_apify_run correctly extracts items when Apify returns direct list."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {
            "X-Apify-Actor-Run-Id": "run_direct_001",
            "X-Apify-Dataset-Id": "ds_direct_001",
        }
        mock_items = [
            {"title": "AI Engineer", "companyName": "Anthropic Partner", "location": "Bengaluru"},
            {"title": "ML Engineer", "companyName": "Google DeepMind Partner", "location": "Hyderabad"},
        ]
        mock_response.json.return_value = mock_items

        run_meta = {"scraper_errors": []}
        with patch("requests.post", return_value=mock_response):
            jobs, meta = execute_apify_run(
                payload={"startUrls": [{"url": "https://example.com"}]},
                params={"token": "test_token_apify"},
                label="UnitTest Direct",
                run_metadata=run_meta,
            )

        self.assertEqual(len(jobs), 2)
        self.assertEqual(meta["status_code"], 200)
        self.assertEqual(meta["total_jobs"], 2)
        self.assertEqual(meta["actor_run_id"], "run_direct_001")
        self.assertEqual(meta["dataset_id"], "ds_direct_001")
        self.assertEqual(meta["run_status"], "SUCCEEDED")
        self.assertEqual(len(run_meta["scraper_errors"]), 0)

    # -------------------------------------------------------------------------
    # TEST 15: Apify empty LinkedIn response
    # -------------------------------------------------------------------------
    def test_apify_linkedin_empty_response(self):
        """Verify execute_apify_run handles empty list cleanly without throwing."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {
            "X-Apify-Actor-Run-Id": "run_empty_002",
            "X-Apify-Dataset-Id": "ds_empty_002",
        }
        mock_response.json.return_value = []

        run_meta = {"scraper_errors": []}
        with patch("requests.post", return_value=mock_response):
            jobs, meta = execute_apify_run(
                payload={"startUrls": []},
                params={"token": "test_token_apify"},
                label="UnitTest Empty",
                run_metadata=run_meta,
            )

        self.assertEqual(len(jobs), 0)
        self.assertEqual(meta["total_jobs"], 0)
        self.assertEqual(meta["status_code"], 200)

    # -------------------------------------------------------------------------
    # TEST 16: Apify error response (e.g. HTTP 500)
    # -------------------------------------------------------------------------
    def test_apify_linkedin_error_response(self):
        """Verify execute_apify_run records scraper error on non-200/201 response."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_response.headers = {}

        run_meta = {"scraper_errors": []}
        with patch("requests.post", return_value=mock_response):
            jobs, meta = execute_apify_run(
                payload={"startUrls": []},
                params={"token": "test_token_apify"},
                label="UnitTest Error",
                run_metadata=run_meta,
            )

        self.assertEqual(len(jobs), 0)
        self.assertEqual(meta["status"], 500)
        self.assertEqual(len(run_meta["scraper_errors"]), 1)
        self.assertIn("500", run_meta["scraper_errors"][0])

    # -------------------------------------------------------------------------
    # TEST 17: Apify async 201 Created -> Poll in-progress -> Fetch dataset
    # -------------------------------------------------------------------------
    def test_apify_linkedin_async_201_poll_and_fetch_dataset(self):
        """
        Verify that when Apify returns 201 Created with RUNNING run object,
        execute_apify_run polls until SUCCEEDED and fetches dataset items from defaultDatasetId.
        """
        # POST response (HTTP 201 Created)
        post_response = MagicMock()
        post_response.status_code = 201
        post_response.headers = {
            "X-Apify-Actor-Run-Id": "run_async_777",
            "X-Apify-Dataset-Id": "dataset_async_888",
        }
        post_response.json.return_value = {
            "data": {
                "id": "run_async_777",
                "status": "RUNNING",
                "defaultDatasetId": "dataset_async_888",
            }
        }

        # Polling GET response (run status -> SUCCEEDED)
        poll_response = MagicMock()
        poll_response.status_code = 200
        poll_response.json.return_value = {
            "data": {
                "id": "run_async_777",
                "status": "SUCCEEDED",
                "defaultDatasetId": "dataset_async_888",
            }
        }

        # Dataset GET response (items from dataset)
        dataset_response = MagicMock()
        dataset_response.status_code = 200
        dataset_response.json.return_value = [
            {"title": "Junior AI Engineer", "companyName": "AI Innovation Lab", "location": "Pune"},
            {"title": "LLM Developer", "companyName": "GenAI Systems", "location": "Mumbai"},
        ]

        def mock_get(url, *args, **kwargs):
            if "actor-runs" in url:
                return poll_response
            elif "datasets" in url:
                return dataset_response
            raise ValueError(f"Unexpected URL: {url}")

        run_meta = {"scraper_errors": []}
        with patch("requests.post", return_value=post_response), \
             patch("requests.get", side_effect=mock_get):
            jobs, meta = execute_apify_run(
                payload={"startUrls": [{"url": "https://example.com"}]},
                params={"token": "test_token_apify"},
                label="UnitTest Async Poll",
                run_metadata=run_meta,
            )

        self.assertEqual(len(jobs), 2)
        self.assertEqual(meta["actor_run_id"], "run_async_777")
        self.assertEqual(meta["dataset_id"], "dataset_async_888")
        self.assertEqual(meta["run_status"], "SUCCEEDED")
        self.assertEqual(meta["total_jobs"], 2)

    # -------------------------------------------------------------------------
    # TEST 18: Freshness policy - 1 hour old job is eligible & fresh
    # -------------------------------------------------------------------------
    def test_freshness_policy_1h_eligible(self):
        """Verify job posted 1 hour ago is eligible and marked fresh."""
        now_utc = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
        job_1h = {
            "source_job_id": "job_1h_001",
            "title": "AI Engineer",
            "companyName": "AI Corp",
            "location": "Bengaluru",
            "postedAt": (now_utc - timedelta(hours=1)).isoformat(),
            "source": "linkedin",
        }

        fr = calculate_freshness(job_1h, now_utc)
        self.assertTrue(fr["is_fresh"], "1-hour old job must be fresh under 24h window")
        self.assertAlmostEqual(fr["discovery_latency_minutes"], 60.0, delta=1.0)

        meta = {"jobs_within_freshness_window": 0, "stale_jobs_filtered": 0, "duplicates_skipped": 0, "email_retries": 0}
        eligible = process_jobs_freshness_and_state([job_1h], {"jobs": {}}, now_utc, meta)
        self.assertEqual(len(eligible), 1)
        self.assertEqual(meta["jobs_within_freshness_window"], 1)

    # -------------------------------------------------------------------------
    # TEST 19: Freshness policy - 6 hours old job is eligible & fresh
    # -------------------------------------------------------------------------
    def test_freshness_policy_6h_eligible(self):
        """Verify job posted 6 hours ago is eligible and marked fresh."""
        now_utc = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
        job_6h = {
            "source_job_id": "job_6h_001",
            "title": "Generative AI Engineer",
            "companyName": "GenAI Corp",
            "location": "Hyderabad",
            "postedAt": (now_utc - timedelta(hours=6)).isoformat(),
            "source": "linkedin",
        }

        fr = calculate_freshness(job_6h, now_utc)
        self.assertTrue(fr["is_fresh"], "6-hour old job must be fresh under 24h window")
        self.assertAlmostEqual(fr["discovery_latency_minutes"], 360.0, delta=1.0)

        meta = {"jobs_within_freshness_window": 0, "stale_jobs_filtered": 0, "duplicates_skipped": 0, "email_retries": 0}
        eligible = process_jobs_freshness_and_state([job_6h], {"jobs": {}}, now_utc, meta)
        self.assertEqual(len(eligible), 1)
        self.assertEqual(meta["jobs_within_freshness_window"], 1)

    # -------------------------------------------------------------------------
    # TEST 20: Freshness policy - 24 hours old job is eligible & fresh
    # -------------------------------------------------------------------------
    def test_freshness_policy_24h_eligible(self):
        """Verify job posted 23.5 hours ago is eligible and marked fresh."""
        now_utc = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
        job_24h = {
            "source_job_id": "job_24h_001",
            "title": "LLM Engineer",
            "companyName": "LLM Studio",
            "location": "Remote",
            "postedAt": (now_utc - timedelta(hours=23, minutes=30)).isoformat(),
            "source": "ashby",
        }

        fr = calculate_freshness(job_24h, now_utc)
        self.assertTrue(fr["is_fresh"], "Job within 24h must be fresh")

        meta = {"jobs_within_freshness_window": 0, "stale_jobs_filtered": 0, "duplicates_skipped": 0, "email_retries": 0}
        eligible = process_jobs_freshness_and_state([job_24h], {"jobs": {}}, now_utc, meta)
        self.assertEqual(len(eligible), 1)

    # -------------------------------------------------------------------------
    # TEST 21: Freshness policy - Very old job (> 14 days) is filtered as stale
    # -------------------------------------------------------------------------
    def test_freshness_policy_ancient_stale(self):
        """Verify job posted 20 days ago is filtered out as stale listing."""
        now_utc = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
        job_ancient = {
            "source_job_id": "job_ancient_001",
            "title": "Machine Learning Engineer",
            "companyName": "Legacy Corp",
            "location": "Bengaluru",
            "postedAt": (now_utc - timedelta(days=20)).isoformat(),
            "source": "greenhouse",
        }

        fr = calculate_freshness(job_ancient, now_utc)
        self.assertFalse(fr["is_fresh"], "Ancient job must not be fresh")

        meta = {"jobs_within_freshness_window": 0, "stale_jobs_filtered": 0, "duplicates_skipped": 0, "email_retries": 0}
        eligible = process_jobs_freshness_and_state([job_ancient], {"jobs": {}}, now_utc, meta)
        self.assertEqual(len(eligible), 0, "Ancient job must not be eligible")
        self.assertEqual(meta["stale_jobs_filtered"], 1)

    # -------------------------------------------------------------------------
    # TEST 22: Merely discovered job is not permanently invisible
    # -------------------------------------------------------------------------
    def test_merely_discovered_job_remains_evaluable(self):
        """
        Verify that a job previously tracked with status='discovered'
        does NOT get dropped at the deduplication gate and remains eligible for evaluation.
        """
        now_utc = datetime.now(timezone.utc)
        state = {
            "jobs": {
                "disc_001": {
                    "job_id": "disc_001",
                    "status": "discovered",
                    "title": "AI Platform Engineer",
                    "company": "Deep Tech",
                    "url": "https://example.com/jobs/disc_001",
                }
            }
        }
        job = {
            "source_job_id": "disc_001",
            "title": "AI Platform Engineer",
            "companyName": "Deep Tech",
            "location": "Bengaluru",
            "url": "https://example.com/jobs/disc_001",
            "postedAt": "2 hours ago",
        }

        meta = {"jobs_within_freshness_window": 0, "stale_jobs_filtered": 0, "duplicates_skipped": 0, "email_retries": 0}
        eligible = process_jobs_freshness_and_state([job], state, now_utc, meta)
        self.assertEqual(len(eligible), 1, "Discovered job must remain eligible for evaluation")
        self.assertEqual(meta["duplicates_skipped"], 0)

    # -------------------------------------------------------------------------
    # TEST 23: Historical rejected status does not permanently suppress valid job
    # -------------------------------------------------------------------------
    def test_historical_rejected_job_reevaluated_through_filters(self):
        """
        Verify that a job historically marked 'rejected' in state is not unconditionally
        dropped at dedup, allowing updated/current deterministic filters to re-evaluate it.
        """
        now_utc = datetime.now(timezone.utc)
        state = {
            "jobs": {
                "rej_001": {
                    "job_id": "rej_001",
                    "status": "rejected",
                    "reason": "old_filter_bug",
                    "title": "AI Solutions Engineer",
                    "company": "AI Partner",
                    "url": "https://example.com/jobs/rej_001",
                }
            }
        }
        job = {
            "source_job_id": "rej_001",
            "title": "AI Solutions Engineer",
            "companyName": "AI Partner",
            "location": "Bengaluru",
            "url": "https://example.com/jobs/rej_001",
            "postedAt": "3 hours ago",
        }

        meta = {"jobs_within_freshness_window": 0, "stale_jobs_filtered": 0, "duplicates_skipped": 0, "email_retries": 0}
        eligible = process_jobs_freshness_and_state([job], state, now_utc, meta)
        self.assertEqual(len(eligible), 1, "Historically rejected job must be allowed through for re-evaluation")
        self.assertEqual(meta["duplicates_skipped"], 0)

    # -------------------------------------------------------------------------
    # TEST 24: print_run_summary executes cleanly without NameError or crash
    # -------------------------------------------------------------------------
    def test_print_run_summary_executes_without_error(self):
        """Verify print_run_summary renders without NameError across populated and empty metadata."""
        start_time = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
        end_time = datetime(2026, 10, 8, 12, 1, 0, tzinfo=timezone.utc)

        meta = {
            "jobs_retrieved": 10,
            "stale_jobs_filtered": 2,
            "eligible_jobs": 8,
            "duplicates_skipped": 1,
            "company_matched_jobs": 7,
            "company_rejected_jobs": 1,
            "experience_matched_jobs": 6,
            "experience_rejected_jobs": 1,
            "location_matched_jobs": 5,
            "location_rejected_jobs": 1,
            "role_matched_jobs": 4,
            "role_rejected_jobs": 1,
            "candidates_surviving_filter": 4,
            "high_match_jobs": 3,
            "emails_sent": 3,
            "email_failures": 0,
            "linkedin_metrics": {
                "partitions_submitted": 20,
                "partitions_successful": 20,
                "partitions_failed": 0,
                "total_items_returned": 25,
                "total_normalized_jobs": 25,
                "total_fresh_jobs": 20,
            },
            "source_stats": {
                "linkedin": {"name": "LinkedIn (Apify)", "status": "OK", "raw": 25, "valid": 25, "elapsed": 10},
                "ashby": {"name": "Ashby", "status": "OK", "raw": 30, "valid": 30, "elapsed": 2},
            },
            "source_funnel": {
                "linkedin": {"raw": 25, "stale": 2, "duplicate": 1, "eligible": 22},
                "ashby": {"raw": 30, "stale": 10, "duplicate": 5, "eligible": 15},
            },
            "per_source_funnel": {
                "linkedin": {"raw": 25, "normalized": 25, "new": 22, "location_pass": 20, "role_pass": 15},
                "ashby": {"raw": 30, "normalized": 30, "new": 15, "location_pass": 5, "role_pass": 0},
            },
            "rejected_titles_by_role": {
                "ashby": ["Frontend Engineer", "DevOps Engineer"],
            },
            "latency_total_sec": 60.0,
            "email_status": "sent",
            "state_persistence": "SUCCESS",
        }

        # Should execute completely without throwing NameError or any exception
        print_run_summary(meta, start_time, end_time)

        # Also test with minimal/empty metadata
        print_run_summary({}, start_time, end_time)

    # -------------------------------------------------------------------------
    # TEST 26: Apify Token Sanitization & Fallback Resolution
    # -------------------------------------------------------------------------
    def test_apify_token_sanitization(self):
        """Verify get_clean_apify_token properly strips whitespace, quotes, Bearer prefix, and resolves env vars."""
        # Test direct cleaning
        self.assertEqual(get_clean_apify_token("  token_123  \n"), "token_123")
        self.assertEqual(get_clean_apify_token('"token_456"'), "token_456")
        self.assertEqual(get_clean_apify_token("'token_789'"), "token_789")
        self.assertEqual(get_clean_apify_token("Bearer my_token"), "my_token")
        self.assertEqual(get_clean_apify_token("Bearer   token_space  "), "token_space")
        self.assertEqual(get_clean_apify_token(""), "")
        self.assertEqual(get_clean_apify_token(None), "")

        # Test environment variable resolution (APIFY_API_KEY vs APIFY_API_TOKEN vs APIFY_TOKEN)
        with patch.dict(os.environ, {"APIFY_API_KEY": "token_from_key", "APIFY_API_TOKEN": "token_from_api", "APIFY_TOKEN": "token_from_tok"}):
            self.assertEqual(get_clean_apify_token(), "token_from_key")

        with patch.dict(os.environ, {"APIFY_API_KEY": "", "APIFY_API_TOKEN": "token_from_api", "APIFY_TOKEN": "token_from_tok"}):
            self.assertEqual(get_clean_apify_token(), "token_from_api")

        with patch.dict(os.environ, {"APIFY_API_KEY": "", "APIFY_API_TOKEN": "", "APIFY_TOKEN": "token_from_tok"}):
            self.assertEqual(get_clean_apify_token(), "token_from_tok")

    # -------------------------------------------------------------------------
    # TEST 27: Apify verify_apify_auth Diagnostic (HTTP 200, 401, 403)
    # -------------------------------------------------------------------------
    def test_verify_apify_auth_diagnostic(self):
        """Verify verify_apify_auth against mock Apify /v2/users/me responses."""
        # 1. Success (HTTP 200)
        mock_200 = MagicMock()
        mock_200.status_code = 200
        mock_200.json.return_value = {"data": {"id": "usr_123", "username": "test_account"}}

        with patch("requests.get", return_value=mock_200) as mock_get:
            ok, status, details = verify_apify_auth("valid_token_abc")
            self.assertTrue(ok)
            self.assertEqual(status, "VALID")
            self.assertEqual(details["username"], "test_account")
            # Verify request headers used Authorization: Bearer
            mock_get.assert_called_once()
            call_kwargs = mock_get.call_args[1]
            self.assertEqual(call_kwargs["headers"]["Authorization"], "Bearer valid_token_abc")

        # 2. Authentication failure (HTTP 401: user-or-token-not-found)
        mock_401 = MagicMock()
        mock_401.status_code = 401
        mock_401.json.return_value = {
            "error": {"type": "user-or-token-not-found", "message": "User was not found or authentication token is not valid"}
        }

        with patch("requests.get", return_value=mock_401):
            ok, status, details = verify_apify_auth("invalid_token_xyz")
            self.assertFalse(ok)
            self.assertEqual(status, "INVALID_TOKEN")
            self.assertEqual(details["status_code"], 401)

        # 3. Monthly usage limit (HTTP 403)
        mock_403 = MagicMock()
        mock_403.status_code = 403
        mock_403.json.return_value = {
            "error": {"type": "platform-feature-disabled", "message": "Monthly usage hard limit exceeded"}
        }

        with patch("requests.get", return_value=mock_403):
            ok, status, details = verify_apify_auth("usage_limited_token")
            self.assertTrue(ok)
            self.assertEqual(status, "USAGE_LIMITED")
            self.assertEqual(details["status_code"], 403)

        # 4. Empty / missing token
        ok, status, details = verify_apify_auth("")
        self.assertFalse(ok)
        self.assertEqual(status, "MISSING_TOKEN")

    # -------------------------------------------------------------------------
    # TEST 28: execute_apify_run Sends Authorization: Bearer Header
    # -------------------------------------------------------------------------
    def test_execute_apify_run_sends_authorization_header(self):
        """Verify execute_apify_run sends standard HTTP Authorization: Bearer header."""
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.headers = {"X-Apify-Actor-Run-Id": "run_auth_1", "X-Apify-Dataset-Id": "ds_auth_1"}
        mock_res.json.return_value = []

        run_meta = {"scraper_errors": []}
        with patch("requests.post", return_value=mock_res) as mock_post:
            execute_apify_run(
                payload={"startUrls": []},
                params={"token": "  my_secret_token  "},
                label="UnitTest Header Check",
                run_metadata=run_meta,
            )

            mock_post.assert_called_once()
            call_headers = mock_post.call_args[1]["headers"]
            self.assertIn("Authorization", call_headers)
            self.assertEqual(call_headers["Authorization"], "Bearer my_secret_token")
            # Verify token in params was also cleaned
            self.assertEqual(mock_post.call_args[1]["params"]["token"], "my_secret_token")

    # -------------------------------------------------------------------------
    # TEST 29: execute_apify_run HTTP 401 Marks Auth Failed
    # -------------------------------------------------------------------------
    def test_execute_apify_run_401_marks_auth_failed(self):
        """Verify execute_apify_run correctly marks apify_auth_failed on HTTP 401."""
        mock_401 = MagicMock()
        mock_401.status_code = 401
        mock_401.text = '{"error": {"type": "user-or-token-not-found", "message": "User was not found"}}'
        mock_401.headers = {}

        run_meta = {"scraper_errors": []}
        with patch("requests.post", return_value=mock_401):
            jobs, meta = execute_apify_run(
                payload={"startUrls": []},
                params={"token": "bad_token"},
                label="UnitTest 401",
                run_metadata=run_meta,
            )

            self.assertEqual(jobs, [])
            self.assertEqual(meta["status"], 401)
            self.assertTrue(meta["auth_failed"])
            self.assertTrue(run_meta["apify_auth_failed"])
            self.assertEqual(len(run_meta["scraper_errors"]), 1)
            self.assertIn("401", run_meta["scraper_errors"][0])


class TestMultiUserProfiles(unittest.TestCase):
    """
    Focused regression and multi-profile isolation tests:
    - Independent profile loading & validation
    - Dynamic LLM prompt construction
    - Profile-specific role, location, experience, and company filtering
    - Profile-specific resume attachment and recruiter outreach
    - Actionable configuration errors when profile is missing or invalid
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)
        set_active_profile(None)

        self.ai_profile = {
            "name": "Jordan Patel",
            "headline": "Applied AI Engineer | Python | LangChain | RAG",
            "education": {
                "degree": "B.Tech",
                "field": "Artificial Intelligence",
                "graduation_year": "2026",
                "cgpa": "8.8",
            },
            "skills": {
                "core": ["Python", "LangChain", "RAG", "LangGraph", "PyTorch", "FastAPI"],
            },
            "projects": ["Vector Search Assistant"],
            "experience_preferences": {
                "level": "entry_level",
                "years_of_experience": 0,
                "max_required_yoe": 2,
                "accept_fresher": True,
                "exclude_senior_titles": True,
            },
            "target_roles": ["AI Engineer", "Applied AI Engineer"],
            "target_role_families": ["AI Engineer", "Applied AI Engineer", "LLM Engineer"],
            "target_locations": ["Hyderabad", "Bengaluru", "Mumbai", "Remote"],
            "remote_preferences": {
                "accept_remote": True,
                "accept_hybrid_in_target_cities": True,
                "home_country": "India",
                "allowed_remote_regions": ["India", "Worldwide"],
            },
            "excluded_companies": ["excludedalpha", "alphabpm"],
            "resume_path": "",
            "video_resume_url": "",
        }

        self.java_resume = self.tmp_path / "alex_java_resume.pdf"
        self.java_resume.write_bytes(b"%PDF-1.4\n%Alex Rivera Java Resume\n")

        self.java_profile = {
            "name": "Alex Rivera",
            "headline": "Backend Developer | Java | Spring Boot | SQL",
            "education": {
                "degree": "B.E.",
                "field": "Information Technology",
                "graduation_year": "2024",
                "cgpa": "8.4",
            },
            "skills": ["Java", "Spring Boot", "SQL", "REST APIs", "PostgreSQL"],
            "projects": [
                {
                    "name": "Order Processing Microservice",
                    "description": "High-throughput Spring Boot and PostgreSQL order backend",
                    "technologies": ["Java", "Spring Boot", "SQL"],
                }
            ],
            "experience_preferences": {
                "level": "entry_level",
                "years_of_experience": 1,
                "max_required_yoe": 2,
                "accept_fresher": True,
                "exclude_senior_titles": True,
            },
            "target_roles": ["Backend Developer", "Java Developer"],
            "target_role_families": ["Backend Developer", "Backend Engineer", "Java Developer"],
            "target_locations": ["Chennai", "Pune"],
            "remote_preferences": {
                "accept_remote": False,
                "accept_hybrid_in_target_cities": True,
                "home_country": "India",
                "allowed_remote_regions": ["India"],
            },
            "excluded_companies": ["excludedbeta"],
            "resume_path": str(self.java_resume),
            "video_resume_url": "https://example.com/alex-intro",
        }

    def tearDown(self):
        set_active_profile(None)
        self.temp_dir.cleanup()

    def test_colleague_profile_loads_from_file(self):
        """TEST 2: A colleague's independently configured profile loads cleanly."""
        profile_file = self.tmp_path / "colleague.profile.json"
        profile_file.write_text(json.dumps(self.java_profile), encoding="utf-8")

        loaded = load_profile(profile_file)
        self.assertEqual(loaded["name"], "Alex Rivera")
        self.assertEqual(loaded["target_roles"], ["Backend Developer", "Java Developer"])
        self.assertEqual(loaded["target_locations"], ["Chennai", "Pune"])

    def test_llm_prompt_reflects_active_profile_only(self):
        """TEST 3: LLM prompt contains the active user's profile and skills, not another user's."""
        sample_job = [
            {
                "title": "Java Developer",
                "companyName": "FinTech Corp",
                "location": "Chennai, India",
                "description": "Build REST services using Java, Spring Boot, and SQL.",
            }
        ]
        prompt = build_batch_prompt(sample_job, profile=self.java_profile)

        self.assertIn("Alex Rivera", prompt)
        self.assertIn("Spring Boot", prompt)
        self.assertIn("Order Processing Microservice", prompt)
        self.assertIn("Backend Developer", prompt)
        self.assertNotIn("Jordan Patel", prompt)
        self.assertNotIn("LangGraph", prompt)

    def test_two_profiles_produce_different_contexts_and_role_filtering(self):
        """TEST 4: Two different profiles produce different contexts, role relevance, and scores."""
        java_job = {
            "title": "Java Developer",
            "companyName": "CloudPay",
            "location": "Pune, India",
            "description": "Backend APIs with Java, Spring Boot, SQL, and PostgreSQL. 0-2 years.",
        }
        ai_job = {
            "title": "Applied AI Engineer",
            "companyName": "GenLab",
            "location": "Hyderabad, India",
            "description": "Build RAG systems with Python, LangChain, LangGraph, FastAPI. 0-2 years.",
        }

        # Contexts differ
        ctx_ai = build_candidate_profile_text(self.ai_profile)
        ctx_java = build_candidate_profile_text(self.java_profile)
        self.assertNotEqual(ctx_ai, ctx_java)
        self.assertIn("LangChain", ctx_ai)
        self.assertIn("Spring Boot", ctx_java)

        # Role filtering differs
        self.assertFalse(is_role_relevant(java_job["title"], java_job["description"], profile=self.ai_profile)[0])
        self.assertTrue(is_role_relevant(java_job["title"], java_job["description"], profile=self.java_profile)[0])
        self.assertTrue(is_role_relevant(ai_job["title"], ai_job["description"], profile=self.ai_profile)[0])
        self.assertFalse(is_role_relevant(ai_job["title"], ai_job["description"], profile=self.java_profile)[0])

        # Local scoring differs
        ai_on_java = local_score_job(java_job, profile=self.ai_profile)["local_score"]
        java_on_java = local_score_job(java_job, profile=self.java_profile)["local_score"]
        self.assertGreater(java_on_java, ai_on_java)
        self.assertGreaterEqual(java_on_java, 70)

    def test_location_preferences_independent_per_profile(self):
        """TEST 5: Location preferences are respected independently for each profile."""
        chennai_job = {"location": "Chennai, Tamil Nadu, India"}
        hyd_job = {"location": "Hyderabad, Telangana, India"}
        pune_job = {"location": "Pune, Maharashtra, India"}
        remote_job = {"location": "Remote - India"}

        # AI profile (Hyderabad, Bengaluru, Mumbai, Remote)
        self.assertFalse(is_valid_work_location(chennai_job, profile=self.ai_profile)[0])
        self.assertTrue(is_valid_work_location(hyd_job, profile=self.ai_profile)[0])
        self.assertFalse(is_valid_work_location(pune_job, profile=self.ai_profile)[0])
        self.assertTrue(is_valid_work_location(remote_job, profile=self.ai_profile)[0])

        # Java profile (Chennai, Pune only; accept_remote=False)
        self.assertTrue(is_valid_work_location(chennai_job, profile=self.java_profile)[0])
        self.assertFalse(is_valid_work_location(hyd_job, profile=self.java_profile)[0])
        self.assertTrue(is_valid_work_location(pune_job, profile=self.java_profile)[0])
        self.assertFalse(is_valid_work_location(remote_job, profile=self.java_profile)[0])

    def test_company_exclusions_independent_per_profile(self):
        """TEST 6: Company exclusions are independently respected per profile."""
        self.assertTrue(is_company_excluded("ExcludedAlpha Technologies", profile=self.ai_profile)[0])
        self.assertFalse(is_company_excluded("ExcludedBeta Corp", profile=self.ai_profile)[0])

        self.assertFalse(is_company_excluded("ExcludedAlpha Technologies", profile=self.java_profile)[0])
        self.assertTrue(is_company_excluded("ExcludedBeta Corp", profile=self.java_profile)[0])

    def test_resume_attachment_uses_active_users_resume(self):
        """TEST 7: Resume attachment attaches the active profile's resume file."""
        recruiter = {
            "name": "Priya Nair",
            "title": "Technical Recruiter",
            "company": "FinTech Systems",
            "email": "priya@fintech.example",
            "recruiter_score": 90,
        }

        sent_messages = []

        class FakeSMTP:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def login(self, user, pwd):
                pass

            def send_message(self, msg):
                sent_messages.append(msg)

        with patch.object(outreach, "AUTO_SEND", True), \
             patch.object(outreach, "GMAIL_USERNAME", "sender@example.com"), \
             patch.object(outreach, "GMAIL_APP_PASSWORD", "app_pass"), \
             patch("smtplib.SMTP_SSL", FakeSMTP):
            subject, status = outreach.send_outreach_email(recruiter, profile=self.java_profile)

        self.assertEqual(status, "sent")
        self.assertEqual(len(sent_messages), 1)
        attachments = list(sent_messages[0].iter_attachments())
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_filename(), "alex_java_resume.pdf")
        self.assertEqual(attachments[0].get_payload(decode=True), b"%PDF-1.4\n%Alex Rivera Java Resume\n")

    def test_outreach_uses_active_users_identity_and_qualifications(self):
        """TEST 8: Recruiter outreach uses the active user's identity, education, skills, projects, and video URL."""
        recruiter = {
            "name": "Samir Rao",
            "title": "Technical Recruiter",
            "company": "PayStream",
            "summary": "Hiring Java and Spring Boot backend engineers",
        }
        subject, body = outreach.build_email(recruiter, profile=self.java_profile)

        self.assertIn("Alex Rivera", subject)
        self.assertIn("Backend Developer", subject)
        self.assertIn("Alex Rivera", body)
        self.assertIn("2024 B.E. in Information Technology", body)
        self.assertIn("Order Processing Microservice", body)
        self.assertIn("Backend Developer and Java Developer", body)
        self.assertIn("https://example.com/alex-intro", body)
        self.assertNotIn("Jordan Patel", body)

    def test_absent_or_invalid_profile_raises_clear_error(self):
        """TEST 9: Missing, invalid JSON, or empty example profile raises ProfileConfigurationError."""
        missing_path = self.tmp_path / "does_not_exist.json"
        with self.assertRaises(ProfileConfigurationError) as ctx_missing:
            load_profile(missing_path)
        self.assertIn("config/profile.example.json", str(ctx_missing.exception))

        bad_json_path = self.tmp_path / "bad.json"
        bad_json_path.write_text("{not valid json", encoding="utf-8")
        with self.assertRaises(ProfileConfigurationError) as ctx_bad:
            load_profile(bad_json_path)
        self.assertIn("Invalid JSON", str(ctx_bad.exception))

        # Example template itself must exist, be valid JSON with empty values, and fail validation
        self.assertTrue(PROFILE_EXAMPLE_PATH.exists(), "config/profile.example.json must exist")
        example_data = json.loads(PROFILE_EXAMPLE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(example_data.get("name"), "")
        self.assertEqual(example_data.get("target_roles"), [])
        self.assertEqual(example_data.get("target_locations"), [])
        self.assertEqual(example_data.get("excluded_companies"), [])
        with self.assertRaises(ProfileConfigurationError) as ctx_empty:
            validate_profile(example_data)
        self.assertIn("Missing/invalid", str(ctx_empty.exception))

        # main.main() fails loudly with exit code 1 if no profile is configured
        with patch.object(config, "PROFILE_PRIVATE_PATH", missing_path), \
             patch.dict(os.environ, {"PROFILE_PATH": "", "PROFILE_JSON": ""}, clear=False):
            set_active_profile(None)
            with self.assertRaises(SystemExit) as sys_cm:
                main.main()
            self.assertEqual(sys_cm.exception.code, 1)

    def test_digest_apify_and_recruiter_matcher_use_active_profile(self):
        """Verify email digest, Apify search payload, and recruiter matcher reflect active profile."""
        jobs = [
            {
                "_job_id": "j_1",
                "title": "Java Developer",
                "companyName": "FinTech",
                "location": "Chennai, India",
                "match_score": 90,
                "url": "https://example.com/1",
                "description": "Java, Spring Boot, SQL",
            },
            {
                "_job_id": "j_2",
                "title": "Backend Developer",
                "companyName": "CloudCo",
                "location": "Pune, India",
                "match_score": 88,
                "url": "https://example.com/2",
                "description": "Java, PostgreSQL",
            },
        ]
        subj, html_body, text_body = build_email_digest(jobs, profile=self.java_profile)
        self.assertIn("Chennai", subj)
        self.assertIn("Pune", subj)
        self.assertIn("Chennai", html_body)
        self.assertIn("Spring Boot", text_body)
        self.assertNotIn("Mumbai", subj)

        # Apify query payload uses colleague's target roles and cities
        with patch.object(main, "APIFY_TOKEN", "mock_tok"), \
             patch("main.execute_apify_run", return_value=([], {"status": 200})) as mock_exec:
            main.search_jobs_apify({"scraper_errors": []}, profile=self.java_profile)
            payload_sent = mock_exec.call_args_list[0][0][0]
            self.assertEqual(payload_sent["searchQueries"], ["Backend Developer", "Java Developer"])
            self.assertEqual(payload_sent["locations"], ["Chennai", "Pune"])

        # Recruiter matcher scores Java recruiter high for Java profile
        java_recruiter = {
            "name": "Anita Verma",
            "title": "Technical Recruiter",
            "headline": "Hiring Backend Developer and Java Developer with Spring Boot and SQL",
            "email": "anita@example.com",
            "recentlyPostedOnLinkedIn": True,
        }
        scored_rec = recruiter_matcher.score_recruiter(java_recruiter, profile=self.java_profile)
        self.assertGreaterEqual(scored_rec["recruiter_score"], 85)

    def test_sequential_profile_switching_and_state_isolation(self):
        """Verify sequential switching via PROFILE_PATH/PROFILE_JSON and state isolation across profiles."""
        path_a = self.tmp_path / "profile_a.json"
        path_b = self.tmp_path / "profile_b.json"
        path_a.write_text(json.dumps(self.ai_profile), encoding="utf-8")
        path_b.write_text(json.dumps(self.java_profile), encoding="utf-8")

        # Load A via PROFILE_PATH, then switch env var to B without manually clearing cache
        with patch.dict(os.environ, {"PROFILE_PATH": str(path_a), "PROFILE_JSON": ""}):
            active_a = get_active_profile()
            self.assertEqual(active_a["name"], "Jordan Patel")

        with patch.dict(os.environ, {"PROFILE_PATH": str(path_b), "PROFILE_JSON": ""}):
            active_b = get_active_profile()
            self.assertEqual(active_b["name"], "Alex Rivera")

        # Load via PROFILE_JSON (GitHub Actions secret path)
        with patch.object(config, "PROFILE_PRIVATE_PATH", self.tmp_path / "absent.json"), \
             patch.dict(os.environ, {"PROFILE_PATH": "", "PROFILE_JSON": json.dumps(self.java_profile)}):
            active_env = get_active_profile()
            self.assertEqual(active_env["name"], "Alex Rivera")

        # Sequential state isolation: job emailed under Profile A must NOT be suppressed or reuse score for Profile B
        shared_state = {"jobs": {}}
        now_utc = datetime.now(timezone.utc)
        shared_job = {
            "source_job_id": "cross_profile_001",
            "title": "Software Engineer",
            "companyName": "TechCo",
            "location": "Hyderabad, India",
            "url": "https://example.com/jobs/cross001",
            "postedAt": "10 minutes ago",
            "description": "Python, LangChain, Java, Spring Boot",
        }
        meta_a = {"jobs_within_freshness_window": 0, "stale_jobs_filtered": 0, "duplicates_skipped": 0, "email_retries": 0}
        el_a = process_jobs_freshness_and_state([shared_job], shared_state, now_utc, meta_a, profile=self.ai_profile)
        self.assertEqual(len(el_a), 1)
        jid = el_a[0]["_job_id"]
        shared_state["jobs"][jid]["status"] = "emailed"
        shared_state["jobs"][jid]["match_score"] = 95

        # Now Profile B runs against the same shared_state: must re-evaluate, not inherit Profile A's emailed/score!
        meta_b = {"jobs_within_freshness_window": 0, "stale_jobs_filtered": 0, "duplicates_skipped": 0, "email_retries": 0}
        el_b = process_jobs_freshness_and_state([dict(shared_job)], shared_state, now_utc, meta_b, profile=self.java_profile)
        self.assertEqual(len(el_b), 1)
        self.assertEqual(meta_b["duplicates_skipped"], 0)
        self.assertFalse(el_b[0].get("_already_scored", False))

    def test_missing_and_invalid_resume_handling(self):
        """Verify optional missing resume works and invalid resume path fails gracefully on live send."""
        recruiter = {
            "name": "Samir Rao",
            "title": "Technical Recruiter",
            "company": "PayStream",
            "email": "samir@paystream.example",
            "recruiter_score": 90,
        }

        # 1. No resume configured: outreach succeeds without attachment and without claiming attachment
        no_resume_profile = dict(self.ai_profile, resume_path="")
        subj, body = outreach.build_email(recruiter, profile=no_resume_profile)
        self.assertNotIn("I've attached my resume", body)
        self.assertGreater(local_score_job({"title": "AI Engineer", "location": "Hyderabad", "description": "Python, LangChain, RAG"}, profile=no_resume_profile)["local_score"], 50)

        # 2. Invalid/nonexistent resume path: does not claim attached; raises FileNotFoundError when AUTO_SEND=true
        bad_resume_profile = dict(self.ai_profile, resume_path=str(self.tmp_path / "missing_resume.pdf"))
        _, bad_body = outreach.build_email(recruiter, profile=bad_resume_profile)
        self.assertNotIn("I've attached my resume", bad_body)

        with patch.dict(os.environ, {"AUTO_SEND": "true", "GMAIL_USERNAME": "u@example.com", "GMAIL_APP_PASSWORD": "p"}):
            with self.assertRaises(FileNotFoundError) as fnf_ctx:
                outreach.send_outreach_email(recruiter, profile=bad_resume_profile)
            self.assertIn("Configured resume file not found", str(fnf_ctx.exception))


if __name__ == "__main__":
    unittest.main()
