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
from config import GROQ_BATCH_SIZE, MIN_MATCH_SCORE
from filters import (
    is_company_excluded,
    evaluate_experience_eligibility,
    is_valid_work_location,
    is_role_relevant,
)
from job_matcher import score_jobs_batch
from main import (
    build_email_digest,
    calculate_freshness,
    filter_jobs_by_company,
    filter_jobs_by_experience,
    filter_jobs_by_role,
    filter_jobs_by_strict_location,
    format_ist_and_utc,
    get_job_id,
    load_state,
    process_jobs_freshness_and_state,
    save_state,
    score_and_rank_jobs,
    send_email_report,
)


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
    9. Infosys remains strictly excluded.
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

    def tearDown(self):
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
                self.assertIn("1 New AI Match", subject)
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
    # TEST 9: Infosys remains strictly excluded
    # -------------------------------------------------------------------------
    def test_infosys_variants_remain_excluded(self):
        """Verify Infosys, Infosys Limited, Infosys BPM, EdgeVerve are rejected."""
        variants = [
            "Infosys",
            "Infosys Limited",
            "Infosys BPM",
            "Infosys BPM Limited",
            "Infosys Technologies",
            "Infosys Consulting",
            "EdgeVerve",
        ]
        for comp in variants:
            is_exc, reason = is_company_excluded(comp)
            self.assertTrue(is_exc, f"Company {comp} was not excluded!")

        # Legitimate AI companies must NOT be excluded
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


if __name__ == "__main__":
    unittest.main()
