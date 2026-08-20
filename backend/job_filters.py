"""
Application-level post-fetch job filtering — pipeline step 1, applied after
both Apify actors' results are merged and mapped, before jobs are inserted.

job_role, location, and posting_timeframe are already passed to the actors as
native input params (best-effort passthrough) and are NOT re-checked here.
company, employment_type, and work_mode are re-checked here because neither
actor reliably enforces them natively (see apify_client.py's NET RESULT note).
"""

import logging

from backend.config import EMPLOYMENT_TYPE_KEYWORDS, WORK_MODE_KEYWORDS

logger = logging.getLogger(__name__)


def _matches_any(text: str, keywords: list[str]) -> bool:
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in keywords)


def _keywords_for(value: str, keyword_map: dict[str, list[str]]) -> list[str]:
    normalized = value.strip().lower().replace(" ", "_")
    return keyword_map.get(normalized, [normalized.replace("_", " ")])


def filter_mapped_jobs(mapped_jobs: list[dict], search_criteria: dict) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Returns (kept, dropped). `dropped` is a list of (job_dict, reason) pairs
    for logging/inspection — the job dicts are the same shape produced by the
    apify_client mapper functions (title, company, description, location,
    work_mode_signal, ...)."""
    company_filter = (search_criteria.get("company") or "").strip().lower()
    employment_type = search_criteria.get("employment_type") or ""
    work_mode = search_criteria.get("work_mode") or ""

    kept: list[dict] = []
    dropped: list[tuple[dict, str]] = []

    for job in mapped_jobs:
        reasons = []

        if company_filter:
            company_value = (job.get("company") or "").lower()
            if company_filter not in company_value:
                reasons.append(f"company {job.get('company')!r} does not contain {search_criteria['company']!r}")

        if employment_type:
            keywords = _keywords_for(employment_type, EMPLOYMENT_TYPE_KEYWORDS)
            text = f"{job.get('title') or ''} {job.get('description') or ''}"
            if not _matches_any(text, keywords):
                reasons.append(f"no employment_type match for {employment_type!r} (looked for {keywords})")

        if work_mode:
            keywords = _keywords_for(work_mode, WORK_MODE_KEYWORDS)
            text = (
                f"{job.get('title') or ''} {job.get('description') or ''} "
                f"{job.get('location') or ''} {job.get('work_mode_signal') or ''}"
            )
            if not _matches_any(text, keywords):
                reasons.append(f"no work_mode match for {work_mode!r} (looked for {keywords})")

        if reasons:
            dropped.append((job, "; ".join(reasons)))
        else:
            kept.append(job)

    if dropped:
        logger.info(
            "Post-fetch filtering: %d/%d jobs kept, %d filtered out",
            len(kept),
            len(mapped_jobs),
            len(dropped),
        )
        for job, reason in dropped:
            logger.info("  filtered out %r (%s): %s", job.get("title"), job.get("company"), reason)

    return kept, dropped
