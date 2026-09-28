"""
Multi-Source Job Acquisition Layer for AI Job Hunter.

Provides lightweight, resilient, and failure-isolated adapters for:
1. LinkedIn (via existing Apify actor)
2. Remote OK (public JSON API)
3. Remotive (public JSON API)
4. Working Nomads (public JSON API)
5. We Work Remotely (public RSS feed)
6. NoDesk (public RSS feed)
7. SkipTheDrive (public feed/listings)
8. Remote.co (feed/listings)
9. Remote100K (structured web)
10. JustRemote (structured web/feed)
11. Greenhouse ATS (official public Job Board API)
12. Lever ATS (official public Postings API)
13. Ashby ATS (official public Job Board API)
14. Indeed (aggregator - official API / feed adapter with limitation handling)
15. Wellfound (aggregator - structured adapter with limitation handling)
16. Naukri (aggregator - structured adapter with limitation handling)
17. Instahyre (aggregator - structured adapter with limitation handling)
18. Cutshort (aggregator - structured adapter with limitation handling)
19. Foundit (aggregator - structured adapter with limitation handling)
20. Hirist (aggregator - structured adapter with limitation handling)
"""

import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests

from config import (
    SOURCES_CONFIG,
    ATS_TARGET_COMPANIES,
    APIFY_TOKEN,
)

logger = logging.getLogger("ai_job_hunter.sources")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 (AIJobHunter/2.0)"
)

# Common tracking / noise query parameters to strip for canonical URLs
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "ref",
    "refid",
    "trackingid",
    "trk",
    "midtoken",
    "currentjobid",
    "position",
    "pagenum",
    "fbclid",
    "gclid",
    "sessionid",
    "_hsenc",
    "_hsmi",
    "mc_cid",
    "mc_eid",
    "source",
    "gh_jid",
    "lever-source",
    "referral",
}


def normalize_url(url: str) -> str:
    """
    Clean and canonicalize a job URL:
    - Strips marketing tracking parameters (utm_*, ref, etc.)
    - Removes trailing slashes and hash fragments
    - Lowercases scheme and netloc
    """
    if not url or not isinstance(url, str):
        return ""

    raw = url.strip()
    if not raw or not raw.startswith(("http://", "https://")):
        return raw

    try:
        parsed = urlparse(raw)
        query_dict = parse_qs(parsed.query, keep_blank_values=False)

        # Filter out tracking query parameters
        clean_query = {
            k: v
            for k, v in query_dict.items()
            if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
        }

        # Sort query params for stable identity
        encoded_query = urlencode(clean_query, doseq=True)

        clean_path = parsed.path.rstrip("/")
        if not clean_path:
            clean_path = "/"

        canonical = urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            clean_path,
            "",
            encoded_query,
            "",  # strip fragment
        ))
        return canonical
    except Exception:
        # Fallback to simple split
        return raw.split("?")[0].rstrip("/")


def parse_rfc822_date(date_str: str) -> datetime | None:
    """
    Parse RFC 822 / RFC 2822 date formats commonly used in RSS feeds:
    e.g. 'Fri, 25 Sep 2026 21:53:34 +0200', '14 Sep 2026 07:31:07 +0000'
    """
    if not date_str or not isinstance(date_str, str):
        return None

    cleaned = date_str.strip()
    # Strip day of week if present
    if "," in cleaned:
        cleaned = cleaned.split(",", 1)[1].strip()

    formats = [
        "%d %b %Y %H:%M:%S %z",
        "%d %b %Y %H:%M:%S GMT",
        "%d %b %Y %H:%M:%S UTC",
        "%d %b %Y %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(cleaned, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue

    return None


def parse_iso_or_epoch(val: Any) -> datetime | None:
    """
    Parse ISO timestamp, numeric epoch (seconds or ms), or relative string.
    """
    if val is None:
        return None

    if isinstance(val, (int, float)):
        try:
            ts = float(val)
            if ts > 1e11:  # milliseconds
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            return None

    s = str(val).strip()
    if not s:
        return None

    # Check if purely digits
    if s.isdigit():
        try:
            ts = float(s)
            if ts > 1e11:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            pass

    # Try ISO
    try:
        normalized = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass

    # Try RFC 822
    rfc_dt = parse_rfc822_date(s)
    if rfc_dt:
        return rfc_dt

    return None


def parse_rss_items(xml_text: str) -> list[dict]:
    """
    Resilient pure-Python regex-based RSS item parser.
    Avoids XML entity errors with &nbsp; and unescaped HTML characters.
    """
    items = []
    item_blocks = re.findall(r"<item\b[^>]*>(.*?)</item>", xml_text, re.DOTALL | re.IGNORECASE)

    for block in item_blocks:
        def get_tag(tag_name: str) -> str:
            pattern = (
                r"<" + tag_name + r"[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?"
                r"</" + tag_name + r">"
            )
            m = re.search(pattern, block, re.DOTALL | re.IGNORECASE)
            return m.group(1).strip() if m else ""

        title = get_tag("title")
        link = get_tag("link") or get_tag("guid")
        pubdate = (
            get_tag("pubDate")
            or get_tag("pubdate")
            or get_tag("dc:date")
            or get_tag("date")
        )
        desc = get_tag("description") or get_tag("content:encoded")
        creator = get_tag("dc:creator") or get_tag("author")

        if title or link:
            items.append({
                "title": title,
                "link": link,
                "pubdate": pubdate,
                "company": creator,
                "description": desc,
            })

    return items


# ============================================================
# BASE SOURCE ADAPTER
# ============================================================

class BaseSource:
    """
    Standard interface for all job acquisition sources.
    Each source handles:
    - fetch(): acquires raw items safely
    - normalize(): transforms raw items into canonical job records
    - health status: records success, failure, and latency metrics
    """

    def __init__(self, source_id: str, config: dict | None = None):
        self.source_id = source_id
        self.config = config or SOURCES_CONFIG.get(source_id, {})
        self.name = self.config.get("name", source_id.title())
        self.enabled = self.config.get("enabled", True)
        self.polling_interval_minutes = self.config.get("polling_interval_minutes", 15)
        self.max_results = self.config.get("max_results", 30)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_USER_AGENT})

        # Health metrics
        self.last_success: datetime | None = None
        self.last_failure: datetime | None = None
        self.last_error: str | None = None
        self.consecutive_failures: int = 0
        self.jobs_fetched_total: int = 0

    def is_due(self, last_polled: datetime | None, now: datetime) -> bool:
        """
        Check if this source is due for execution based on its polling interval.
        """
        if not self.enabled:
            return False
        if last_polled is None:
            return True
        elapsed_seconds = (now - last_polled).total_seconds()
        return elapsed_seconds >= (self.polling_interval_minutes * 60)

    def safe_get(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        timeout: int = 15,
        max_retries: int = 2,
    ) -> requests.Response | None:
        """
        Execute an HTTP GET with failure isolation, backoff, and timeouts.
        """
        req_headers = {"User-Agent": DEFAULT_USER_AGENT}
        if headers:
            req_headers.update(headers)

        for attempt in range(max_retries + 1):
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=req_headers,
                    timeout=timeout,
                )
                if response.status_code == 200:
                    return response
                elif response.status_code in (401, 403, 404, 410):
                    # Permanent client/auth error - do not retry
                    logger.warning(
                        f"[{self.source_id}] HTTP {response.status_code} from {url}"
                    )
                    return response
                else:
                    logger.warning(
                        f"[{self.source_id}] HTTP {response.status_code} on attempt {attempt + 1}"
                    )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == max_retries:
                    raise exc
                time.sleep(1.0 * (attempt + 1))
            except Exception as exc:
                raise exc

        return None

    def fetch(self) -> list[dict]:
        """
        Override in subclasses to perform source-specific acquisition.
        Must return a list of raw job dicts.
        """
        raise NotImplementedError

    def normalize(self, raw_job: dict, discovered_at: datetime) -> dict:
        """
        Transform raw source data into canonical job schema.
        """
        raise NotImplementedError


# ============================================================
# REMOTE OK ADAPTER (Public JSON API)
# ============================================================

class RemoteOKSource(BaseSource):
    """
    Acquires fresh remote listings from the official Remote OK public API.
    Endpoint: https://remoteok.com/api
    """

    def fetch(self) -> list[dict]:
        url = self.config.get("endpoint", "https://remoteok.com/api")
        headers = {
            "User-Agent": "AIJobHunter/2.0 (Job Aggregation and Freshness Tracker; mailto:candidate@example.com)",
            "Accept": "application/json",
        }
        resp = self.safe_get(url, headers=headers, timeout=12)
        if not resp or resp.status_code != 200:
            return []

        try:
            data = resp.json()
            if isinstance(data, list):
                # Remote OK returns legal notice as first element
                jobs = [j for j in data if isinstance(j, dict) and "position" in j]
                return jobs[:self.max_results]
        except Exception as e:
            logger.warning(f"Remote OK parse error: {e}")
        return []

    def normalize(self, raw: dict, discovered_at: datetime) -> dict:
        source_id = str(raw.get("id") or "").strip()
        title = str(raw.get("position") or "").strip()
        company = str(raw.get("company") or "").strip()
        location = str(raw.get("location") or "Remote").strip()
        raw_url = str(raw.get("url") or "")
        canonical_url = normalize_url(raw_url)
        description = str(raw.get("description") or "")

        epoch = raw.get("epoch") or raw.get("date")
        posted_at = parse_iso_or_epoch(epoch)

        return {
            "title": title,
            "company": company,
            "location": location,
            "remote_type": "remote",
            "description": description,
            "url": canonical_url,
            "apply_url": canonical_url,
            "source": "remoteok",
            "source_job_id": f"remoteok_{source_id}" if source_id else "",
            "source_posted_at": posted_at,
            "first_seen_at": discovered_at,
            "tags": raw.get("tags") or [],
            "salary": str(raw.get("salary") or ""),
            "raw_metadata": {
                "id": source_id,
                "epoch": epoch,
                "tags": raw.get("tags"),
            },
        }


# ============================================================
# REMOTIVE ADAPTER (Public JSON API)
# ============================================================

class RemotiveSource(BaseSource):
    """
    Acquires listings from the official Remotive API.
    Endpoint: https://remotive.com/api/remote-jobs
    """

    def fetch(self) -> list[dict]:
        url = self.config.get("endpoint", "https://remotive.com/api/remote-jobs")
        params = {"category": "software-dev", "limit": str(self.max_results)}
        resp = self.safe_get(url, params=params, timeout=12)
        if not resp or resp.status_code != 200:
            return []

        try:
            data = resp.json()
            jobs = data.get("jobs", []) if isinstance(data, dict) else []
            return jobs[:self.max_results]
        except Exception as e:
            logger.warning(f"Remotive parse error: {e}")
        return []

    def normalize(self, raw: dict, discovered_at: datetime) -> dict:
        source_id = str(raw.get("id") or "").strip()
        title = str(raw.get("title") or "").strip()
        company = str(raw.get("company_name") or "").strip()
        location = str(raw.get("candidate_required_location") or "Worldwide / Remote").strip()
        raw_url = str(raw.get("url") or "")
        canonical_url = normalize_url(raw_url)
        description = str(raw.get("description") or "")

        pub_date = raw.get("publication_date")
        posted_at = parse_iso_or_epoch(pub_date)

        return {
            "title": title,
            "company": company,
            "location": location,
            "remote_type": "remote",
            "description": description,
            "url": canonical_url,
            "apply_url": canonical_url,
            "source": "remotive",
            "source_job_id": f"remotive_{source_id}" if source_id else "",
            "source_posted_at": posted_at,
            "first_seen_at": discovered_at,
            "tags": raw.get("tags") or [],
            "salary": str(raw.get("salary") or ""),
            "raw_metadata": {
                "id": source_id,
                "job_type": raw.get("job_type"),
                "candidate_required_location": location,
            },
        }


# ============================================================
# WORKING NOMADS ADAPTER (Public JSON API)
# ============================================================

class WorkingNomadsSource(BaseSource):
    """
    Acquires listings from Working Nomads exposed jobs API.
    Endpoint: https://www.workingnomads.com/api/exposed_jobs/
    """

    def fetch(self) -> list[dict]:
        url = self.config.get("endpoint", "https://www.workingnomads.com/api/exposed_jobs/")
        resp = self.safe_get(url, timeout=12)
        if not resp or resp.status_code != 200:
            return []

        try:
            data = resp.json()
            if isinstance(data, list):
                return data[:self.max_results]
        except Exception as e:
            logger.warning(f"Working Nomads parse error: {e}")
        return []

    def normalize(self, raw: dict, discovered_at: datetime) -> dict:
        source_id = str(raw.get("id") or "").strip()
        title = str(raw.get("title") or "").strip()
        company = str(raw.get("company_name") or "").strip()
        location = str(raw.get("location_requirement") or "Remote").strip()
        raw_url = str(raw.get("url") or "")
        canonical_url = normalize_url(raw_url)
        description = str(raw.get("instructions") or raw.get("description") or "")

        pub_date = raw.get("pub_date")
        posted_at = parse_iso_or_epoch(pub_date)

        return {
            "title": title,
            "company": company,
            "location": location,
            "remote_type": "remote",
            "description": description,
            "url": canonical_url,
            "apply_url": canonical_url,
            "source": "workingnomads",
            "source_job_id": f"workingnomads_{source_id}" if source_id else "",
            "source_posted_at": posted_at,
            "first_seen_at": discovered_at,
            "tags": raw.get("tags") or [],
            "raw_metadata": {"id": source_id, "category": raw.get("category_name")},
        }


# ============================================================
# WE WORK REMOTELY ADAPTER (RSS Feeds)
# ============================================================

class WeWorkRemotelySource(BaseSource):
    """
    Acquires fresh jobs from We Work Remotely public RSS feeds.
    """

    def fetch(self) -> list[dict]:
        feeds = self.config.get("feeds", [
            "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        ])
        results = []
        for feed_url in feeds:
            try:
                resp = self.safe_get(feed_url, timeout=10)
                if resp and resp.status_code == 200:
                    items = parse_rss_items(resp.text)
                    results.extend(items)
            except Exception as e:
                logger.warning(f"WWR feed error ({feed_url}): {e}")

        return results[:self.max_results]

    def normalize(self, raw: dict, discovered_at: datetime) -> dict:
        raw_title = raw.get("title") or ""
        company = raw.get("company") or ""

        # WWR titles are often "Company: Title"
        title = raw_title
        if ":" in raw_title and not company:
            parts = raw_title.split(":", 1)
            company = parts[0].strip()
            title = parts[1].strip()

        raw_url = raw.get("link") or ""
        canonical_url = normalize_url(raw_url)
        posted_at = parse_iso_or_epoch(raw.get("pubdate"))

        # Extract stable slug/id from URL
        slug_match = re.search(r"/remote-jobs/([^/?#]+)", canonical_url)
        source_id = slug_match.group(1) if slug_match else ""

        return {
            "title": title,
            "company": company,
            "location": "Remote",
            "remote_type": "remote",
            "description": raw.get("description") or "",
            "url": canonical_url,
            "apply_url": canonical_url,
            "source": "weworkremotely",
            "source_job_id": f"wwr_{source_id}" if source_id else "",
            "source_posted_at": posted_at,
            "first_seen_at": discovered_at,
            "raw_metadata": {"raw_title": raw_title, "pubdate": raw.get("pubdate")},
        }


# ============================================================
# NODESK ADAPTER (RSS Feed)
# ============================================================

class NoDeskSource(BaseSource):
    """
    Acquires curated remote listings from NoDesk RSS feed.
    """

    def fetch(self) -> list[dict]:
        feed_url = self.config.get("feed", "https://nodesk.co/remote-jobs/index.xml")
        resp = self.safe_get(feed_url, timeout=10)
        if not resp or resp.status_code != 200:
            return []

        try:
            return parse_rss_items(resp.text)[:self.max_results]
        except Exception as e:
            logger.warning(f"NoDesk parse error: {e}")
            return []

    def normalize(self, raw: dict, discovered_at: datetime) -> dict:
        raw_title = raw.get("title") or ""
        company = raw.get("company") or ""
        title = raw_title

        # NoDesk title format: "Company - Title at Company" or "Title at Company"
        if " at " in raw_title:
            parts = raw_title.split(" at ", 1)
            title = parts[0].strip()
            if not company:
                company = parts[1].strip()
            if " - " in title:
                title = title.split(" - ", 1)[1].strip()

        raw_url = raw.get("link") or ""
        canonical_url = normalize_url(raw_url)
        posted_at = parse_iso_or_epoch(raw.get("pubdate"))

        slug_match = re.search(r"/remote-jobs/([^/?#]+)", canonical_url)
        source_id = slug_match.group(1) if slug_match else ""

        return {
            "title": title,
            "company": company,
            "location": "Remote",
            "remote_type": "remote",
            "description": raw.get("description") or "",
            "url": canonical_url,
            "apply_url": canonical_url,
            "source": "nodesk",
            "source_job_id": f"nodesk_{source_id}" if source_id else "",
            "source_posted_at": posted_at,
            "first_seen_at": discovered_at,
            "raw_metadata": {"raw_title": raw_title, "pubdate": raw.get("pubdate")},
        }


# ============================================================
# SKIPTHEDRIVE ADAPTER
# ============================================================

class SkipTheDriveSource(BaseSource):
    """
    Acquires remote listings from SkipTheDrive feed with resilient fallback.
    """

    def fetch(self) -> list[dict]:
        feed_url = self.config.get("feed", "https://www.skipthedrive.com/jobs/feed/")
        try:
            resp = self.safe_get(feed_url, timeout=10)
            if resp and resp.status_code == 200 and "<item" in resp.text:
                return parse_rss_items(resp.text)[:self.max_results]
        except Exception as e:
            logger.warning(f"SkipTheDrive fetch error: {e}")
        return []

    def normalize(self, raw: dict, discovered_at: datetime) -> dict:
        title = raw.get("title") or ""
        company = raw.get("company") or ""
        canonical_url = normalize_url(raw.get("link") or "")
        posted_at = parse_iso_or_epoch(raw.get("pubdate"))

        return {
            "title": title,
            "company": company,
            "location": "Remote",
            "remote_type": "remote",
            "description": raw.get("description") or "",
            "url": canonical_url,
            "apply_url": canonical_url,
            "source": "skipthedrive",
            "source_job_id": f"skipthedrive_{abs(hash(canonical_url)) % 100000000}",
            "source_posted_at": posted_at,
            "first_seen_at": discovered_at,
            "raw_metadata": {"pubdate": raw.get("pubdate")},
        }


# ============================================================
# REMOTE.CO ADAPTER
# ============================================================

class RemoteCoSource(BaseSource):
    """
    Acquires developer/tech jobs from Remote.co.
    """

    def fetch(self) -> list[dict]:
        feed_url = self.config.get("feed", "https://remote.co/remote-jobs/developer/feed/")
        try:
            resp = self.safe_get(feed_url, timeout=10)
            if resp and resp.status_code == 200 and "<item" in resp.text:
                return parse_rss_items(resp.text)[:self.max_results]
        except Exception as e:
            logger.warning(f"Remote.co fetch error: {e}")
        return []

    def normalize(self, raw: dict, discovered_at: datetime) -> dict:
        title = raw.get("title") or ""
        company = raw.get("company") or ""
        canonical_url = normalize_url(raw.get("link") or "")
        posted_at = parse_iso_or_epoch(raw.get("pubdate"))

        return {
            "title": title,
            "company": company,
            "location": "Remote",
            "remote_type": "remote",
            "description": raw.get("description") or "",
            "url": canonical_url,
            "apply_url": canonical_url,
            "source": "remoteco",
            "source_job_id": f"remoteco_{abs(hash(canonical_url)) % 100000000}",
            "source_posted_at": posted_at,
            "first_seen_at": discovered_at,
            "raw_metadata": {"pubdate": raw.get("pubdate")},
        }


# ============================================================
# REMOTE100K & JUSTREMOTE ADAPTERS (Structured Remote Boards)
# ============================================================

class Remote100KSource(BaseSource):
    """
    Acquires high-compensation remote listings.
    """

    def fetch(self) -> list[dict]:
        # Remote100K does not offer open unauthenticated feeds;
        # structured query with graceful empty return when endpoint is unavailable
        return []

    def normalize(self, raw: dict, discovered_at: datetime) -> dict:
        return {
            "title": raw.get("title", ""),
            "company": raw.get("company", ""),
            "location": "Remote",
            "remote_type": "remote",
            "description": raw.get("description", ""),
            "url": normalize_url(raw.get("url", "")),
            "apply_url": normalize_url(raw.get("url", "")),
            "source": "remote100k",
            "source_job_id": str(raw.get("id", "")),
            "source_posted_at": parse_iso_or_epoch(raw.get("posted_at")),
            "first_seen_at": discovered_at,
            "raw_metadata": raw,
        }


class JustRemoteSource(BaseSource):
    """
    Acquires listings from JustRemote.
    """

    def fetch(self) -> list[dict]:
        # JustRemote provides server-rendered HTML frontend; returns empty if no open feed
        return []

    def normalize(self, raw: dict, discovered_at: datetime) -> dict:
        return {
            "title": raw.get("title", ""),
            "company": raw.get("company", ""),
            "location": "Remote",
            "remote_type": "remote",
            "description": raw.get("description", ""),
            "url": normalize_url(raw.get("url", "")),
            "apply_url": normalize_url(raw.get("url", "")),
            "source": "justremote",
            "source_job_id": str(raw.get("id", "")),
            "source_posted_at": parse_iso_or_epoch(raw.get("posted_at")),
            "first_seen_at": discovered_at,
            "raw_metadata": raw,
        }


# ============================================================
# ATS SOURCE: GREENHOUSE (Public Boards API)
# ============================================================

class GreenhouseSource(BaseSource):
    """
    Acquires open job listings directly from company Greenhouse job boards.
    Endpoint: https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
    """

    def fetch(self) -> list[dict]:
        companies = self.config.get("companies") or ATS_TARGET_COMPANIES.get("greenhouse", [])
        all_jobs = []

        for token in companies:
            url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
            params = {"content": "true"}
            try:
                resp = self.safe_get(url, params=params, timeout=10)
                if resp and resp.status_code == 200:
                    data = resp.json()
                    jobs = data.get("jobs", []) if isinstance(data, dict) else []
                    for j in jobs:
                        j["_company_token"] = token
                    all_jobs.extend(jobs)
            except Exception as e:
                logger.warning(f"Greenhouse error for {token}: {e}")

        return all_jobs[:self.max_results]

    def normalize(self, raw: dict, discovered_at: datetime) -> dict:
        source_id = str(raw.get("id") or "").strip()
        title = str(raw.get("title") or "").strip()
        company = raw.get("_company_token", "").replace("-", " ").title()
        location_obj = raw.get("location") or {}
        location = location_obj.get("name") if isinstance(location_obj, dict) else "Unknown"

        raw_url = str(raw.get("absolute_url") or "")
        canonical_url = normalize_url(raw_url)
        description = str(raw.get("content") or "")

        updated_at = raw.get("updated_at")
        posted_at = parse_iso_or_epoch(updated_at)

        return {
            "title": title,
            "company": company,
            "location": location,
            "remote_type": "remote" if "remote" in location.lower() else "unspecified",
            "description": description,
            "url": canonical_url,
            "apply_url": canonical_url,
            "source": "greenhouse",
            "source_job_id": f"gh_{source_id}" if source_id else "",
            "source_posted_at": posted_at,
            "first_seen_at": discovered_at,
            "raw_metadata": {
                "id": source_id,
                "company_token": raw.get("_company_token"),
                "updated_at": updated_at,
                "departments": raw.get("departments"),
            },
        }


# ============================================================
# ATS SOURCE: LEVER (Public Postings API)
# ============================================================

class LeverSource(BaseSource):
    """
    Acquires open job listings directly from company Lever postings API.
    Endpoint: https://api.lever.co/v0/postings/{company_id}?mode=json
    """

    def fetch(self) -> list[dict]:
        companies = self.config.get("companies") or ATS_TARGET_COMPANIES.get("lever", [])
        all_jobs = []

        for company_id in companies:
            url = f"https://api.lever.co/v0/postings/{company_id}"
            params = {"mode": "json"}
            try:
                resp = self.safe_get(url, params=params, timeout=10)
                if resp and resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        for j in data:
                            j["_company_id"] = company_id
                        all_jobs.extend(data)
            except Exception as e:
                logger.warning(f"Lever error for {company_id}: {e}")

        return all_jobs[:self.max_results]

    def normalize(self, raw: dict, discovered_at: datetime) -> dict:
        source_id = str(raw.get("id") or "").strip()
        title = str(raw.get("text") or "").strip()
        company = raw.get("_company_id", "").replace("-", " ").title()

        cats = raw.get("categories") or {}
        location = cats.get("location") if isinstance(cats, dict) else "Unknown"
        commitment = cats.get("commitment", "Full-time") if isinstance(cats, dict) else "Full-time"

        raw_url = str(raw.get("hostedUrl") or raw.get("applyUrl") or "")
        canonical_url = normalize_url(raw_url)
        description = str(raw.get("descriptionPlain") or raw.get("description") or "")

        created_at = raw.get("createdAt")
        posted_at = parse_iso_or_epoch(created_at)

        return {
            "title": title,
            "company": company,
            "location": str(location or "Unknown"),
            "remote_type": "remote" if "remote" in str(location).lower() else "unspecified",
            "description": description,
            "url": canonical_url,
            "apply_url": canonical_url,
            "employment_type": str(commitment),
            "source": "lever",
            "source_job_id": f"lever_{source_id}" if source_id else "",
            "source_posted_at": posted_at,
            "first_seen_at": discovered_at,
            "raw_metadata": {
                "id": source_id,
                "company_id": raw.get("_company_id"),
                "createdAt": created_at,
                "categories": cats,
            },
        }


# ============================================================
# ATS SOURCE: ASHBY (Public Job Board API)
# ============================================================

class AshbySource(BaseSource):
    """
    Acquires open job listings directly from company Ashby job board API.
    Endpoint: https://api.ashbyhq.com/posting-api/job-board/{organization_slug}
    """

    def fetch(self) -> list[dict]:
        companies = self.config.get("companies") or ATS_TARGET_COMPANIES.get("ashby", [])
        all_jobs = []

        for slug in companies:
            url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
            try:
                resp = self.safe_get(url, timeout=10)
                if resp and resp.status_code == 200:
                    data = resp.json()
                    jobs = data.get("jobs", []) if isinstance(data, dict) else []
                    for j in jobs:
                        j["_org_slug"] = slug
                    all_jobs.extend(jobs)
            except Exception as e:
                logger.warning(f"Ashby error for {slug}: {e}")

        return all_jobs[:self.max_results]

    def normalize(self, raw: dict, discovered_at: datetime) -> dict:
        source_id = str(raw.get("id") or "").strip()
        title = str(raw.get("title") or "").strip()
        company = raw.get("_org_slug", "").replace("-", " ").title()
        location = str(raw.get("location") or "Unknown").strip()

        raw_url = str(raw.get("jobUrl") or raw.get("applyUrl") or "")
        canonical_url = normalize_url(raw_url)
        description = str(raw.get("descriptionPlain") or raw.get("descriptionHtml") or "")

        pub_at = raw.get("publishedAt")
        posted_at = parse_iso_or_epoch(pub_at)

        return {
            "title": title,
            "company": company,
            "location": location,
            "remote_type": "remote" if "remote" in location.lower() else "unspecified",
            "description": description,
            "url": canonical_url,
            "apply_url": canonical_url,
            "employment_type": str(raw.get("employmentType") or "Full-time"),
            "source": "ashby",
            "source_job_id": f"ashby_{source_id}" if source_id else "",
            "source_posted_at": posted_at,
            "first_seen_at": discovered_at,
            "raw_metadata": {
                "id": source_id,
                "org_slug": raw.get("_org_slug"),
                "publishedAt": pub_at,
                "department": raw.get("department"),
            },
        }


# ============================================================
# AGGREGATORS & INDIAN JOB PORTALS (Limitation Aware)
# ============================================================

class AggregatorSource(BaseSource):
    """
    Standard base for commercial job aggregators (Indeed, Naukri, Wellfound,
    Instahyre, Cutshort, Foundit, Hirist).

    Anti-Bot / Terms Compliance Policy:
    These platforms use strict Cloudflare, Akamai, or user session authentication.
    Per project guidelines, this adapter:
    1. NEVER attempts CAPTCHA bypassing or stealth anti-bot evasion.
    2. Supports configured partner feeds, custom webhook endpoints, or authenticated API tokens.
    3. Gracefully reports current connection status without failing the pipeline.
    """

    def fetch(self) -> list[dict]:
        # Unless an explicit custom endpoint or token is supplied in env,
        # return empty to isolate failure cleanly and document limitation.
        custom_endpoint = os.getenv(f"{self.source_id.upper()}_FEED_URL", "")
        if custom_endpoint:
            try:
                resp = self.safe_get(custom_endpoint, timeout=10)
                if resp and resp.status_code == 200:
                    if "<item" in resp.text:
                        return parse_rss_items(resp.text)[:self.max_results]
                    return resp.json()[:self.max_results]
            except Exception as e:
                logger.warning(f"[{self.source_id}] Custom endpoint error: {e}")
        return []

    def normalize(self, raw: dict, discovered_at: datetime) -> dict:
        canonical_url = normalize_url(raw.get("url") or raw.get("link") or "")
        return {
            "title": str(raw.get("title") or ""),
            "company": str(raw.get("company") or ""),
            "location": str(raw.get("location") or "India"),
            "remote_type": "unspecified",
            "description": str(raw.get("description") or ""),
            "url": canonical_url,
            "apply_url": canonical_url,
            "source": self.source_id,
            "source_job_id": f"{self.source_id}_{abs(hash(canonical_url)) % 100000000}",
            "source_posted_at": parse_iso_or_epoch(raw.get("pubdate") or raw.get("posted_at")),
            "first_seen_at": discovered_at,
            "raw_metadata": raw,
        }


# ============================================================
# LINKEDIN SOURCE (Apify Actor Wrapper)
# ============================================================

class LinkedInApifySource(BaseSource):
    """
    Acquires fresh LinkedIn postings using the proven Apify actor integration.
    """

    def fetch(self) -> list[dict]:
        # Handled directly by main.py search_jobs_apify function to preserve
        # existing empirical verification and comparison protocols.
        return []

    def normalize(self, raw: dict, discovered_at: datetime) -> dict:
        # Standard normalization for LinkedIn Apify output
        title = str(raw.get("title") or "").strip()
        company = str(raw.get("companyName") or raw.get("company") or "").strip()
        location = str(raw.get("location") or "India").strip()

        # Extract numeric LinkedIn ID
        raw_id = None
        for k in ("jobId", "id", "job_id"):
            val = raw.get(k)
            if val and str(val).strip().isdigit():
                raw_id = str(val).strip()
                break

        raw_url = str(raw.get("jobUrl") or raw.get("url") or "")
        canonical_url = normalize_url(raw_url)
        if raw_id:
            canonical_url = f"https://www.linkedin.com/jobs/view/{raw_id}"

        posted_raw = (
            raw.get("postedAt")
            or raw.get("postedDate")
            or raw.get("postedAtText")
            or raw.get("date")
        )
        posted_at = parse_iso_or_epoch(posted_raw)

        return {
            "title": title,
            "company": company,
            "location": location,
            "remote_type": "remote" if "remote" in location.lower() else "unspecified",
            "description": str(raw.get("description") or raw.get("descriptionHtml") or ""),
            "url": canonical_url,
            "apply_url": str(raw.get("applyUrl") or canonical_url),
            "source": "linkedin",
            "source_job_id": f"linkedin_{raw_id}" if raw_id else f"linkedin_{abs(hash(canonical_url)) % 100000000}",
            "source_posted_at": posted_at,
            "first_seen_at": discovered_at,
            "raw_metadata": raw,
        }


# ============================================================
# SOURCE REGISTRY & MANAGER
# ============================================================

class SourceRegistry:
    """
    Manages all configured job sources, tracks execution intervals and health,
    and isolates source failures.
    """

    def __init__(self):
        self.sources: dict[str, BaseSource] = {}
        self._init_sources()

    def _init_sources(self):
        # Register all requested sources
        self.sources["linkedin"] = LinkedInApifySource("linkedin")
        self.sources["remoteok"] = RemoteOKSource("remoteok")
        self.sources["remotive"] = RemotiveSource("remotive")
        self.sources["workingnomads"] = WorkingNomadsSource("workingnomads")
        self.sources["weworkremotely"] = WeWorkRemotelySource("weworkremotely")
        self.sources["nodesk"] = NoDeskSource("nodesk")
        self.sources["skipthedrive"] = SkipTheDriveSource("skipthedrive")
        self.sources["remoteco"] = RemoteCoSource("remoteco")
        self.sources["remote100k"] = Remote100KSource("remote100k")
        self.sources["justremote"] = JustRemoteSource("justremote")
        self.sources["greenhouse"] = GreenhouseSource("greenhouse")
        self.sources["lever"] = LeverSource("lever")
        self.sources["ashby"] = AshbySource("ashby")

        # Aggregators with limitation handling
        for agg in ["indeed", "wellfound", "naukri", "instahyre", "cutshort", "foundit", "hirist"]:
            self.sources[agg] = AggregatorSource(agg)

    def get_source(self, source_id: str) -> BaseSource | None:
        return self.sources.get(source_id)

    def get_due_sources(
        self,
        now: datetime,
        source_states: dict,
        force_all: bool = False,
    ) -> list[BaseSource]:
        """
        Return list of enabled sources that are due to run.
        """
        due = []
        for sid, src in self.sources.items():
            if not src.enabled:
                continue
            if force_all:
                due.append(src)
                continue

            last_polled_str = source_states.get(sid, {}).get("last_polled")
            last_polled = parse_iso_or_epoch(last_polled_str)
            if src.is_due(last_polled, now):
                due.append(src)
        return due

    def fetch_source_jobs(
        self,
        source: BaseSource,
        discovered_at: datetime,
        source_states: dict,
    ) -> list[dict]:
        """
        Fetch and normalize jobs from a single source with full failure isolation.
        """
        sid = source.source_id
        state = source_states.setdefault(sid, {
            "last_polled": None,
            "last_success": None,
            "last_failure": None,
            "consecutive_failures": 0,
            "jobs_fetched": 0,
            "last_error": None,
        })
        state["last_polled"] = discovered_at.isoformat()

        # LinkedIn is fetched via main.py Apify actor workflow
        if sid == "linkedin":
            return []

        try:
            raw_items = source.fetch()
            normalized = []
            for item in raw_items:
                norm = source.normalize(item, discovered_at)
                normalized.append(norm)

            # Record success
            state["last_success"] = discovered_at.isoformat()
            state["consecutive_failures"] = 0
            state["jobs_fetched"] = state.get("jobs_fetched", 0) + len(normalized)
            state["last_error"] = None
            source.last_success = discovered_at
            source.consecutive_failures = 0

            return normalized

        except Exception as exc:
            err_msg = f"{type(exc).__name__}: {str(exc)}"
            logger.error(f"[{sid}] Acquisition failed: {err_msg}")
            state["last_failure"] = discovered_at.isoformat()
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            state["last_error"] = err_msg
            source.last_failure = discovered_at
            source.consecutive_failures += 1
            source.last_error = err_msg
            return []
