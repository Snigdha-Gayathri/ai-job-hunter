from config import (
    MAX_EMAILS_PER_RUN,
    MAX_RECRUITERS_TO_CONTACT,
    MIN_EMAIL_SCORE,
    OUTREACH_STATE_FILE,
    RECRUITER_STATE_FILE,
)

from outreach import (
    send_outreach_email,
)

from recruiter_discovery import (
    search_recruiters,
)

from recruiter_matcher import (
    filter_and_rank_recruiters,
)

from recruiter_state import (
    RecruiterState,
)


def main():
    print("=" * 70)
    print("AI RECRUITER HUNTER STARTED")
    print("=" * 70)

    print()
    print(
        "API strategy:"
    )

    print(
        "  Apify: ONE recruiter discovery request"
    )

    print(
        "  Groq: ZERO requests"
    )

    print(
        "  Email enrichment: included in Apify request"
    )

    print(
        "  Matching: local"
    )

    # ========================================================
    # STATE
    # ========================================================

    state = RecruiterState(
        recruiter_file=RECRUITER_STATE_FILE,
        outreach_file=OUTREACH_STATE_FILE,
    )

    print()
    print(
        f"Previously seen recruiters: "
        f"{len(state.recruiters)}"
    )

    print(
        f"Previously contacted emails: "
        f"{len(state.outreach)}"
    )

    # ========================================================
    # DISCOVERY
    # ========================================================

    recruiters = search_recruiters()

    if not recruiters:
        print()
        print(
            "No recruiter profiles returned."
        )

        state.save()

        return

    # ========================================================
    # NORMALIZE + FILTER
    # ========================================================

    print()
    print("=" * 70)
    print("LOCAL RECRUITER MATCHING")
    print("=" * 70)

    ranked = filter_and_rank_recruiters(
        recruiters
    )

    print(
        f"Recruiters meeting "
        f"minimum score: {len(ranked)}"
    )

    # ========================================================
    # REMOVE PREVIOUSLY CONTACTED PEOPLE
    # ========================================================

    eligible = []

    previously_seen = 0
    already_contacted = 0
    no_email = 0

    for recruiter in ranked:

        # Always record the recruiter as seen.
        state.record_seen(
            recruiter
        )

        if state.has_been_contacted(
            recruiter
        ):
            already_contacted += 1
            continue

        if not recruiter.get(
            "email"
        ):
            no_email += 1
            continue

        score = recruiter.get(
            "recruiter_score",
            0,
        )

        if score < MIN_EMAIL_SCORE:
            continue

        eligible.append(
            recruiter
        )

    print()
    print(
        f"Already contacted: "
        f"{already_contacted}"
    )

    print(
        f"No usable email: "
        f"{no_email}"
    )

    print(
        f"Eligible for outreach: "
        f"{len(eligible)}"
    )

    # ========================================================
    # RANK OUTREACH TARGETS
    # ========================================================

    eligible.sort(
        key=lambda recruiter: recruiter.get(
            "recruiter_score",
            0,
        ),
        reverse=True,
    )

    targets = eligible[
        :min(
            MAX_RECRUITERS_TO_CONTACT,
            MAX_EMAILS_PER_RUN,
        )
    ]

    # ========================================================
    # DISPLAY TARGETS
    # ========================================================

    print()
    print("=" * 70)
    print("OUTREACH TARGETS")
    print("=" * 70)

    if not targets:
        print(
            "No new recruiter qualifies for outreach."
        )

    for index, recruiter in enumerate(
        targets,
        start=1,
    ):
        print()
        print(
            f"{index}. "
            f"{recruiter.get('name', 'Unknown')}"
        )

        print(
            f"   Title: "
            f"{recruiter.get('title', 'Unknown')}"
        )

        print(
            f"   Company: "
            f"{recruiter.get('company', 'Unknown')}"
        )

        print(
            f"   Score: "
            f"{recruiter.get('recruiter_score', 0)}/100"
        )

        print(
            f"   Email: "
            f"{recruiter.get('email', 'None')}"
        )

        print(
            f"   LinkedIn: "
            f"{recruiter.get('linkedin_url', 'None')}"
        )

        reasons = recruiter.get(
            "score_reasons",
            [],
        )

        for reason in reasons[:4]:
            print(
                f"   + {reason}"
            )

    # ========================================================
    # SEND
    # ========================================================

    sent_count = 0

    for recruiter in targets:

        try:
            subject, status = (
                send_outreach_email(
                    recruiter
                )
            )

            state.record_outreach(
                recruiter,
                subject,
                status=status,
            )

            if status == "sent":
                sent_count += 1

        except Exception as error:
            print()
            print(
                f"Outreach failed for "
                f"{recruiter.get('name', 'Unknown')}: "
                f"{error}"
            )

            state.record_outreach(
                recruiter,
                subject="FAILED",
                status=f"failed: {error}",
            )

    # ========================================================
    # SAVE
    # ========================================================

    state.save()

    # ========================================================
    # SUMMARY
    # ========================================================

    print()
    print("=" * 70)
    print("RECRUITER HUNTER SUMMARY")
    print("=" * 70)

    print(
        f"Profiles discovered: "
        f"{len(recruiters)}"
    )

    print(
        f"Relevant recruiters: "
        f"{len(ranked)}"
    )

    print(
        f"Outreach candidates: "
        f"{len(eligible)}"
    )

    print(
        f"Emails sent: "
        f"{sent_count}"
    )

    print()
    print(
        "AI RECRUITER HUNTER COMPLETED"
    )

    print("=" * 70)


if __name__ == "__main__":
    main()
