from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from backend.activity import log_activity
from backend.apify_client import map_linkedin_job, search_linkedin_jobs
from backend.database import engine
from backend.job_filters import filter_mapped_jobs
from backend.models import Campaign, Job, utcnow
from backend.qualification import qualify_job

# One entry per source actor: (source label, search fn, mapper fn).
#
# LinkedIn only for now: Dice doesn't respect the `location` search criteria
# at all (it ignores it and returns generic US-based remote postings
# regardless of what was asked for, e.g. a "India" search coming back with
# only Tampa/NJ/Minnesota/LA jobs) — worse than not having a second source.
# Re-add Dice here (and to the JOB_SOURCES-shaped import above) once/if that
# actor's location handling is sorted out.
JOB_SOURCES = [
    ("linkedin", search_linkedin_jobs, map_linkedin_job),
]


def run_campaign_search(campaign_id: int) -> None:
    """BackgroundTask entry point: pending -> running -> completed|failed.

    Runs in its own DB session since the request-scoped session from the
    endpoint that scheduled this task is already closed by the time it runs.
    Calls every source in JOB_SOURCES and merges results into the same `jobs`
    table (tagged via `source`). The campaign only fails outright if EVERY
    source errors out — if one source fails but another succeeds, we keep the
    jobs we got and log the partial failure instead of discarding good data.
    """
    with Session(engine) as session:
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            return

        campaign.status = "running"
        session.add(campaign)
        session.commit()

        all_raw_jobs: list[tuple[str, dict]] = []
        source_errors: dict[str, str] = {}

        for source, search_fn, _mapper in JOB_SOURCES:
            try:
                raw_jobs = search_fn(campaign.search_criteria)
                all_raw_jobs.extend((source, raw_job) for raw_job in raw_jobs)
            except Exception as exc:
                source_errors[source] = str(exc)

        if len(source_errors) == len(JOB_SOURCES):
            campaign.status = "failed"
            session.add(campaign)
            session.commit()
            reasons = "; ".join(f"{source}: {msg}" for source, msg in source_errors.items())
            log_activity(session, "campaign", campaign_id, f"failed: {reasons}")
            return

        mapper_by_source = {source: mapper for source, _search_fn, mapper in JOB_SOURCES}
        mapped_jobs = [mapper_by_source[source](raw_job) for source, raw_job in all_raw_jobs]

        # Application-level filtering for the fields the actors don't reliably
        # enforce natively (company, employment_type, work_mode) — applied once
        # on the merged set, before anything is inserted. job_role, location,
        # and posting_timeframe are already enforced natively by the actors.
        fetched_count = len(mapped_jobs)
        kept_jobs, dropped_jobs = filter_mapped_jobs(mapped_jobs, campaign.search_criteria)

        inserted = 0
        for job_data in kept_jobs:
            job = Job(campaign_id=campaign_id, **job_data)
            session.add(job)
            try:
                session.commit()
            except IntegrityError:
                # Duplicate (campaign_id, external_job_id) — dedup is M7's job; skip for now.
                session.rollback()
                continue
            session.refresh(job)
            inserted += 1
            # Qualify immediately, one job at a time. qualify_job() never raises —
            # an AI failure is written as status="pending" so one bad call can't
            # take down the rest of the batch or flip the campaign to "failed".
            qualify_job(session, job)

        campaign.status = "completed"
        campaign.last_checked_at = utcnow()
        session.add(campaign)
        session.commit()

        summary = f"completed: {fetched_count} fetched, {len(dropped_jobs)} filtered out, {inserted} inserted"
        if source_errors:
            summary += " (partial failure: " + "; ".join(f"{s}: {m}" for s, m in source_errors.items()) + ")"
        log_activity(session, "campaign", campaign_id, summary)
