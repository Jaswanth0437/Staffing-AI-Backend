"""
Apify integration — pipeline step 1 (job search).

Two actors, merged into the same `jobs` table (tagged via the `source` column):

1. LinkedIn — ivanvs/linkedin-job-scraper (actor ID 7ZuNFntlWSa1LO5uG)
   Store page: https://apify.com/ivanvs/linkedin-job-scraper

   Input schema fields we use:
     keywords          string  - job title / search term
     location          string  - e.g. "New York"
     employmentTypes   array   - employment type filter (best-effort passthrough — the
                                 actor's accepted enum values aren't fully documented,
                                 so an unrecognized value is passed through as-is and
                                 the actor will most likely just ignore it rather than error)
     experienceLevel   string  - seniority filter (same best-effort caveat as above)
     datePosted        string  - format "{number} {unit}", e.g. "7 days"
     maxRecords        integer - result cap
   Not used: contractType (semantics overlap with employmentTypes and aren't
   documented clearly enough to map both without risking a conflicting filter),
   geoId, distance, companyId (company filter needs LinkedIn's numeric company ID,
   not a name — `search_criteria.company` can't be mapped without an ID lookup step),
   salary, extractCompanyData, jobIds.
   NO native "work_mode" (remote/hybrid/onsite) filter in this actor's input schema.

   Output fields (subset we map into `jobs` columns):
     id, url, title, shortTitle, company {name, url, logo},
     location {city, region, country, coordinates}, description (HTML),
     employmentType, seniorityLevel, applicants (int), datePosted (ISO),
     salary, baseSalary {min, max}
   Provides `applicants` (→ applicant_count) but NO company-size field.

2. Dice — parsebird/dice-jobs-scraper (actor ID 1UN4uJf9NsNPb4VxN)
   Store page: https://apify.com/parsebird/dice-jobs-scraper

   Input schema fields we use:
     keyword      string  - job title / skill / role
     location     string  - city/state/region, or "Remote"
     postedDate   string  - enum: all | 24h | 3d | 7d | 30d
     maxResults   integer - result cap
   Not used: startUrl, maxPages, proxyConfiguration.
   NO native fields for employment_type, experience_level, or work_mode as a
   distinct filter — the only lever is overloading `location` with "Remote",
   which this actor's own docs treat as the way to scope to remote jobs.

   Output fields (subset we map into `jobs` columns):
     id / jobId / dice_id, title, companyName (or company), location,
     employmentType, posted / updated, description_text / description_html / summary, url,
     workSetting (e.g. "On-Site" / "Remote" / "Hybrid" — undocumented but present in
     real output; captured as-is into `work_mode_signal`, not normalized or filtered on yet)
   Provides NEITHER applicant count NOR company size.

NET RESULT on the open question: neither actor exposes employment_type, work_mode,
or posting_timeframe as clean, fully-enum-documented native filters the way the old
Indeed actor's schema at least tried to. LinkedIn takes a stab at employment_type/
experience_level/posting_timeframe but the accepted values aren't spelled out in its
docs (best-effort passthrough here); Dice only really gives us posting_timeframe
natively (`postedDate`) plus the "Remote" location convention for work_mode. Still no
actor here does query-param URL construction for us the way Indeed's `startUrls`
would have — if these filters need to be guaranteed, the fallback is still manually
building actor-specific start URLs, not something either of these does automatically.

On the applicant_count / company_employee_size gap that was totally missing for
Indeed: LinkedIn's `applicants` field unblocks applicant_count. Company employee
size remains unavailable from every source wired up so far — M2's floor-check
against `EMPLOYEE_SIZE_FLOOR` will stay a no-op (skipped, not failed) until a
source that provides it gets added.
"""

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Optional

import requests

from backend.config import MAX_JOBS_PER_CAMPAIGN, MAX_JOBS_PER_CAMPAIGN_WITH_COMPANY_FILTER, settings

APIFY_RUN_SYNC_URL = "https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items"

_DICE_POSTED_DATE_MAP = {
    "any": "all",
    "all_time": "all",
    "today": "24h",
    "last_24_hours": "24h",
    "last_3_days": "3d",
    "last_7_days": "7d",
    "last_week": "7d",
    "last_30_days": "30d",
    "last_month": "30d",
}


def _run_actor_sync(actor_id: str, actor_input: dict) -> list[dict]:
    """Runs any Apify actor synchronously and returns its raw dataset items."""
    if not settings.APIFY_TOKEN:
        raise RuntimeError("APIFY_TOKEN is not set — add it to .env before running a campaign.")
    if not actor_id:
        raise RuntimeError("Actor ID is not set — add LINKEDIN_ACTOR_ID / DICE_ACTOR_ID to .env.")

    response = requests.post(
        APIFY_RUN_SYNC_URL.format(actor_id=actor_id),
        params={"token": settings.APIFY_TOKEN},
        json=actor_input,
        timeout=300,
    )
    response.raise_for_status()
    return response.json()


def _external_job_id(raw_job: dict, *id_keys: str) -> str:
    """Best-effort stable ID: the first present native key, else the job URL, else a
    hash of the whole payload."""
    for key in id_keys:
        if raw_job.get(key):
            return str(raw_job[key])
    if raw_job.get("url"):
        return str(raw_job["url"])
    return hashlib.sha1(json.dumps(raw_job, sort_keys=True, default=str).encode()).hexdigest()


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _linkedin_date_posted(posting_timeframe: Optional[str]) -> Optional[str]:
    """Best-effort translation of our generic posting_timeframe into the
    "{number} {unit}" string this actor's datePosted field expects."""
    if not posting_timeframe:
        return None
    match = re.search(r"(\d+)\s*[_\s]?(hour|day|week|month)", posting_timeframe, re.IGNORECASE)
    if not match:
        return None
    number, unit = match.groups()
    return f"{number} {unit.lower()}s"


# --- LinkedIn (ivanvs/linkedin-job-scraper) ---


def build_linkedin_input(search_criteria: dict) -> dict:
    max_records = MAX_JOBS_PER_CAMPAIGN_WITH_COMPANY_FILTER if search_criteria.get("company") else MAX_JOBS_PER_CAMPAIGN
    actor_input: dict[str, Any] = {
        "keywords": search_criteria.get("job_role") or "",
        "location": search_criteria.get("location") or "",
        "maxRecords": max_records,
    }
    if search_criteria.get("employment_type"):
        actor_input["employmentTypes"] = [search_criteria["employment_type"]]
    if search_criteria.get("experience_level"):
        actor_input["experienceLevel"] = search_criteria["experience_level"]
    date_posted = _linkedin_date_posted(search_criteria.get("posting_timeframe"))
    if date_posted:
        actor_input["datePosted"] = date_posted
    return actor_input


def search_linkedin_jobs(search_criteria: dict) -> list[dict]:
    return _run_actor_sync(settings.LINKEDIN_ACTOR_ID, build_linkedin_input(search_criteria))


def map_linkedin_job(raw_job: dict) -> dict[str, Any]:
    company = raw_job.get("company")
    company_name = company.get("name") if isinstance(company, dict) else company

    location = raw_job.get("location")
    if isinstance(location, dict):
        location_str = ", ".join(
            part for part in (location.get("city"), location.get("region"), location.get("country")) if part
        )
    else:
        location_str = location

    return {
        "external_job_id": _external_job_id(raw_job, "id"),
        "title": raw_job.get("shortTitle") or raw_job.get("title"),
        "company": company_name,
        "location": location_str,
        "description": raw_job.get("description"),
        "posted_date": _parse_iso_datetime(raw_job.get("datePosted")),
        "applicant_count": raw_job.get("applicants"),
        "company_employee_size": None,  # not provided by this actor
        "source": "linkedin",
        "work_mode_signal": None,  # this actor's output has no remote/onsite/hybrid field
        "raw_data": raw_job,
    }


# --- Dice (parsebird/dice-jobs-scraper) ---


def build_dice_input(search_criteria: dict) -> dict:
    location = search_criteria.get("location") or ""
    if search_criteria.get("work_mode") == "remote" and not location:
        location = "Remote"  # this actor's own convention for scoping to remote jobs

    max_results = MAX_JOBS_PER_CAMPAIGN_WITH_COMPANY_FILTER if search_criteria.get("company") else MAX_JOBS_PER_CAMPAIGN
    actor_input: dict[str, Any] = {
        "keyword": search_criteria.get("job_role") or "",
        "location": location,
        "maxResults": max_results,
    }
    posting_timeframe = (search_criteria.get("posting_timeframe") or "").lower()
    actor_input["postedDate"] = _DICE_POSTED_DATE_MAP.get(posting_timeframe, "all")
    return actor_input


def search_dice_jobs(search_criteria: dict) -> list[dict]:
    return _run_actor_sync(settings.DICE_ACTOR_ID, build_dice_input(search_criteria))


def map_dice_job(raw_job: dict) -> dict[str, Any]:
    return {
        "external_job_id": _external_job_id(raw_job, "id", "jobId", "dice_id"),
        "title": raw_job.get("title"),
        "company": raw_job.get("companyName") or raw_job.get("company"),
        "location": raw_job.get("location"),
        "description": raw_job.get("description_text") or raw_job.get("description_html") or raw_job.get("summary"),
        "posted_date": _parse_iso_datetime(raw_job.get("posted") or raw_job.get("updated")),
        "applicant_count": None,  # not provided by this actor
        "company_employee_size": None,  # not provided by this actor
        "source": "dice",
        "work_mode_signal": raw_job.get("workSetting"),  # e.g. "On-Site" / "Remote" / "Hybrid"
        "raw_data": raw_job,
    }
