from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from backend.activity import log_activity
from backend.apify_client import map_dice_job, map_linkedin_job, search_dice_jobs, search_linkedin_jobs
from backend.database import engine
from backend.job_filters import filter_mapped_jobs
from backend.models import Campaign, Job, utcnow
from backend.qualification import qualify_job

# Every source actor this app knows how to call: (source label, search fn,
# mapper fn). Which of these actually run for a given campaign is chosen by
# its search_criteria.job_source ("linkedin" | "dice" | "both") — see
# _sources_for() below.
#
# Note on Dice: it doesn't respect the `location` search criteria at all (it
# ignores it and returns generic US-based remote postings regardless of what
# was asked for, e.g. a "India" search coming back with only Tampa/NJ/
# Minnesota/LA jobs). It's included here because job_source makes running it
# an explicit per-campaign opt-in rather than something silently forced on
# every search — "linkedin" stays the default for campaigns that don't set
# job_source at all.
JOB_SOURCES = [
    ("linkedin", search_linkedin_jobs, map_linkedin_job),
    ("dice", search_dice_jobs, map_dice_job),
]


def _sources_for(job_source: str | None) -> list[tuple[str, object, object]]:
    """Selects which of JOB_SOURCES a campaign actually searches.

    Defaults to LinkedIn-only for any campaign missing/with an unrecognized
    job_source — that's every campaign created before this field existed, so
    this default is what keeps them behaving exactly as before."""
    if job_source == "dice":
        return [entry for entry in JOB_SOURCES if entry[0] == "dice"]
    if job_source == "both":
        return JOB_SOURCES
    return [entry for entry in JOB_SOURCES if entry[0] == "linkedin"]


def run_campaign_search(campaign_id: int) -> None:
    """BackgroundTask entry point: pending|completed -> running -> completed|failed.

    Runs in its own DB session since the request-scoped session from the
    endpoint that scheduled this task is already closed by the time it runs.
    Calls whichever JOB_SOURCES entries search_criteria.job_source selects
    (see _sources_for()) and merges results into the same `jobs` table
    (tagged via `source`). The campaign only fails outright if EVERY active
    source errors out — if one source fails but another succeeds, we keep the
    jobs we got and log the partial failure instead of discarding good data.

    Doubles as the recheck entry point (POST /campaigns/{id}/recheck calls
    this on an already-`completed` campaign) — the (campaign_id,
    external_job_id) unique constraint means a job already on file is simply
    skipped (see the IntegrityError catch below), so only genuinely new
    postings get inserted. Newly-inserted jobs are flagged `is_new=True`; any
    `is_new` flag left over from an earlier run is cleared first so only the
    latest batch is ever marked.
    """
    with Session(engine) as session:
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            return

        campaign.status = "running"
        session.add(campaign)
        session.commit()

        stale_new_jobs = session.exec(select(Job).where(Job.campaign_id == campaign_id, Job.is_new == True)).all()  # noqa: E712
        for stale_job in stale_new_jobs:
            stale_job.is_new = False
            session.add(stale_job)
        session.commit()

        active_sources = _sources_for(campaign.search_criteria.get("job_source"))

        all_raw_jobs: list[tuple[str, dict]] = []
        source_errors: dict[str, str] = {}

        for source, search_fn, _mapper in active_sources:
            try:
                raw_jobs = search_fn(campaign.search_criteria)
                all_raw_jobs.extend((source, raw_job) for raw_job in raw_jobs)
            except Exception as exc:
                source_errors[source] = str(exc)

        if len(source_errors) == len(active_sources):
            campaign.status = "failed"
            campaign.last_run_summary = {"fetched": 0, "inserted": 0, "filtered_out": 0, "errors": source_errors}
            session.add(campaign)
            session.commit()
            reasons = "; ".join(f"{source}: {msg}" for source, msg in source_errors.items())
            log_activity(session, "campaign", campaign_id, f"failed: {reasons}")
            return

        mapper_by_source = {source: mapper for source, _search_fn, mapper in active_sources}
        mapped_jobs = [mapper_by_source[source](raw_job) for source, raw_job in all_raw_jobs]

        # Application-level filtering for the fields the actors don't reliably
        # enforce natively (company, employment_type, work_mode) — applied once
        # on the merged set, before anything is inserted. job_role, location,
        # and posting_timeframe are already enforced natively by the actors.
        fetched_count = len(mapped_jobs)
        kept_jobs, dropped_jobs = filter_mapped_jobs(mapped_jobs, campaign.search_criteria)

        inserted = 0
        for job_data in kept_jobs:
            job = Job(campaign_id=campaign_id, is_new=True, **job_data)
            session.add(job)
            try:
                session.commit()
            except IntegrityError:
                # Duplicate (campaign_id, external_job_id) — already on file
                # from an earlier run (initial search or a prior recheck).
                session.rollback()
                continue
            session.refresh(job)
            inserted += 1
            # Qualify immediately, one job at a time. qualify_job() never raises —
            # an AI failure is written as status="pending" so one bad call can't
            # take down the rest of the batch or flip the campaign to "failed".
            qualify_job(session, job)

        # Per-category counts of *why* jobs were dropped — a job with
        # reasons in more than one category (e.g. wrong employment_type AND
        # wrong work_mode) counts toward each category it actually failed,
        # so these can sum to more than len(dropped_jobs). Lets the UI
        # explain an empty jobs list instead of it looking identical to
        # "nothing was ever found" (see the Jobs card's empty state).
        filter_reason_counts: dict[str, int] = {}
        for _job, reasons in dropped_jobs:
            for category in {category for category, _message in reasons}:
                filter_reason_counts[category] = filter_reason_counts.get(category, 0) + 1

        campaign.status = "completed"
        campaign.last_checked_at = utcnow()
        campaign.last_run_summary = {
            "fetched": fetched_count,
            "inserted": inserted,
            "filtered_out": len(dropped_jobs),
            "reasons": filter_reason_counts,
            "errors": source_errors,
        }
        session.add(campaign)
        session.commit()

        summary = f"completed: {fetched_count} fetched, {len(dropped_jobs)} filtered out, {inserted} inserted"
        if source_errors:
            summary += " (partial failure: " + "; ".join(f"{s}: {m}" for s, m in source_errors.items()) + ")"
        log_activity(session, "campaign", campaign_id, summary)
