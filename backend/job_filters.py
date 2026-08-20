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

# The opposite bucket for each work mode — used only when a job has no
# structured work_mode_signal (i.e. every LinkedIn job; Dice does provide
# one). A LinkedIn posting's title/description usually does NOT explicitly
# say "onsite" even when it is (unlike "remote", which postings do tend to
# state), so requiring a positive keyword match drops nearly everything.
# Instead: only reject when the text contradicts the requested mode: reject
# an onsite request if the job explicitly says remote, and vice versa.
# Ambiguous cases (neither mentioned, or "hybrid" requested) are kept rather
# than dropped — same "missing data is not a failure" philosophy as the
# qualification rules' null-skip checks.
_WORK_MODE_CONTRADICTS = {
    "remote": "onsite",
    "onsite": "remote",
}


def _matches_any(text: str, keywords: list[str]) -> bool:
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in keywords)


def _keywords_for(value: str, keyword_map: dict[str, list[str]]) -> list[str]:
    normalized = value.strip().lower().replace(" ", "_")
    return keyword_map.get(normalized, [normalized.replace("_", " ")])


# LinkedIn's `employmentType` output enum doesn't line up 1:1 with our own
# tokens — confirmed live: it's "CONTRACTOR", not "CONTRACT" (this was
# silently rejecting every genuine contract match before this alias was
# added). Only entries that differ from a plain snake_case normalization
# need listing here.
_EMPLOYMENT_TYPE_SIGNAL_ALIASES = {
    "contractor": "contract",
}


def _normalize_employment_type(value: str | None) -> str:
    normalized = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _EMPLOYMENT_TYPE_SIGNAL_ALIASES.get(normalized, normalized)


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
            # LinkedIn's `employmentType` OUTPUT field (job.employment_type_signal)
            # is reliable — unlike the `employmentTypes` INPUT filter we send the
            # actor, which live testing showed it mostly ignores (requesting
            # contract-only still returned ~90% FULL_TIME jobs). When present,
            # trust it directly instead of guessing from free-text keywords,
            # which was rejecting genuine matches whose text just didn't happen
            # to say "contract" verbatim.
            signal = _normalize_employment_type(job.get("employment_type_signal"))
            if signal:
                normalized_requested = _normalize_employment_type(employment_type)
                if signal != normalized_requested:
                    reasons.append(f"employment_type_signal {signal!r} does not match requested {employment_type!r}")
            else:
                keywords = _keywords_for(employment_type, EMPLOYMENT_TYPE_KEYWORDS)
                text = f"{job.get('title') or ''} {job.get('description') or ''}"
                if not _matches_any(text, keywords):
                    reasons.append(f"no employment_type match for {employment_type!r} (looked for {keywords})")

        if work_mode:
            normalized_mode = work_mode.strip().lower().replace(" ", "_")
            text = (
                f"{job.get('title') or ''} {job.get('description') or ''} "
                f"{job.get('location') or ''} {job.get('work_mode_signal') or ''}"
            )
            if job.get("work_mode_signal"):
                # Real structured signal (Dice) — enforce a positive match.
                keywords = _keywords_for(work_mode, WORK_MODE_KEYWORDS)
                if not _matches_any(text, keywords):
                    reasons.append(f"no work_mode match for {work_mode!r} (looked for {keywords})")
            else:
                # No structured signal (LinkedIn) — only reject on an explicit
                # contradiction; ambiguous/unstated is kept, not dropped.
                opposite = _WORK_MODE_CONTRADICTS.get(normalized_mode)
                if opposite:
                    opposite_keywords = _keywords_for(opposite, WORK_MODE_KEYWORDS)
                    if _matches_any(text, opposite_keywords):
                        reasons.append(f"job text explicitly says {opposite!r}, contradicting the requested {work_mode!r}")

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
