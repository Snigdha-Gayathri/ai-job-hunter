import json
from datetime import datetime, timezone
from pathlib import Path


def utc_now():
    return datetime.now(
        timezone.utc
    ).isoformat()


class RecruiterState:
    """
    Persistent state for recruiter discovery
    and outreach.

    Structure:

    recruiters.json
        recruiter_id -> recruiter metadata

    outreach_history.json
        email -> outreach metadata
    """

    def __init__(
        self,
        recruiter_file: Path,
        outreach_file: Path,
    ):
        self.recruiter_file = recruiter_file
        self.outreach_file = outreach_file

        self.recruiter_file.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.recruiters = self._load(
            self.recruiter_file
        )

        self.outreach = self._load(
            self.outreach_file
        )

    # ========================================================
    # FILE I/O
    # ========================================================

    @staticmethod
    def _load(path):
        if not path.exists():
            return {}

        try:
            with path.open(
                "r",
                encoding="utf-8",
            ) as file:
                data = json.load(file)

            if isinstance(data, dict):
                return data

        except Exception as error:
            print(
                f"WARNING: Could not load "
                f"{path}: {error}"
            )

        return {}

    @staticmethod
    def _save(path, data):
        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temporary = path.with_suffix(
            path.suffix + ".tmp"
        )

        with temporary.open(
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                data,
                file,
                indent=2,
                ensure_ascii=False,
            )

        temporary.replace(path)

    # ========================================================
    # RECRUITER IDENTIFICATION
    # ========================================================

    @staticmethod
    def recruiter_id(recruiter):
        """
        Generate a stable identifier.

        Prefer LinkedIn URL.
        Fall back to email.
        Fall back to name + company.
        """

        linkedin = (
            recruiter.get("linkedin_url")
            or recruiter.get("linkedinUrl")
            or recruiter.get("url")
        )

        if linkedin:
            return str(linkedin).strip().lower()

        email = recruiter.get("email")

        if email:
            return str(email).strip().lower()

        name = str(
            recruiter.get("name")
            or ""
        ).strip().lower()

        company = str(
            recruiter.get("company")
            or recruiter.get("companyName")
            or ""
        ).strip().lower()

        return (
            f"{name}|{company}"
        )

    # ========================================================
    # SEEN
    # ========================================================

    def has_seen(self, recruiter):
        recruiter_id = self.recruiter_id(
            recruiter
        )

        return recruiter_id in self.recruiters

    def record_seen(self, recruiter):
        recruiter_id = self.recruiter_id(
            recruiter
        )

        existing = self.recruiters.get(
            recruiter_id,
            {},
        )

        now = utc_now()

        self.recruiters[
            recruiter_id
        ] = {
            **existing,
            "name": recruiter.get(
                "name",
                existing.get(
                    "name",
                    "",
                ),
            ),
            "company": recruiter.get(
                "company",
                existing.get(
                    "company",
                    "",
                ),
            ),
            "title": recruiter.get(
                "title",
                existing.get(
                    "title",
                    "",
                ),
            ),
            "linkedin_url": recruiter.get(
                "linkedin_url",
                existing.get(
                    "linkedin_url",
                    "",
                ),
            ),
            "email": recruiter.get(
                "email",
                existing.get(
                    "email",
                    "",
                ),
            ),
            "first_seen": existing.get(
                "first_seen",
                now,
            ),
            "last_seen": now,
        }

    # ========================================================
    # OUTREACH
    # ========================================================

    def has_been_contacted(self, recruiter):
        email = (
            recruiter.get("email")
            or ""
        ).strip().lower()

        if not email:
            return False

        return email in self.outreach

    def record_outreach(
        self,
        recruiter,
        subject,
        status="sent",
    ):
        email = (
            recruiter.get("email")
            or ""
        ).strip().lower()

        if not email:
            return

        self.outreach[email] = {
            "name": recruiter.get(
                "name",
                "",
            ),
            "company": recruiter.get(
                "company",
                "",
            ),
            "linkedin_url": recruiter.get(
                "linkedin_url",
                "",
            ),
            "subject": subject,
            "status": status,
            "sent_at": utc_now(),
        }

    # ========================================================
    # SAVE
    # ========================================================

    def save(self):
        self._save(
            self.recruiter_file,
            self.recruiters,
        )

        self._save(
            self.outreach_file,
            self.outreach,
        )

        print(
            f"Recruiter state saved: "
            f"{len(self.recruiters)} recruiters"
        )

        print(
            f"Outreach history saved: "
            f"{len(self.outreach)} contacts"
        )
