from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import Session, select

from backend.activity import log_activity
from backend.apify_client import clean_html_description
from backend.campaign_service import run_campaign_search
from backend.database import get_session
from backend.models import Campaign, Contact, Email, EmployeeMatch, Job, Lead, Qualification
from backend.schemas import CampaignCreate, CampaignCreateResponse, JobOut

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


def _normalize_criteria(criteria: dict) -> dict:
    """Normalizes a search_criteria dict for duplicate comparison — null,
    blank, and differently-cased/whitespace-padded values should all count
    as "the same", not a false negative on the duplicate check."""
    normalized = {}
    for key, value in criteria.items():
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip().lower()
            if not value:
                continue
        normalized[key] = value
    return normalized


def _find_duplicate_campaign(session: Session, search_criteria: dict) -> Campaign | None:
    target = _normalize_criteria(search_criteria)
    for campaign in session.exec(select(Campaign)).all():
        if _normalize_criteria(campaign.search_criteria) == target:
            return campaign
    return None


@router.post("", response_model=CampaignCreateResponse)
def create_campaign(
    payload: CampaignCreate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    search_criteria = payload.search_criteria.model_dump()

    duplicate = _find_duplicate_campaign(session, search_criteria)
    if duplicate is not None:
        return CampaignCreateResponse(campaign=duplicate, is_duplicate=True)

    campaign = Campaign(
        name=payload.name,
        search_criteria=search_criteria,
        status="pending",
    )
    session.add(campaign)
    session.commit()

    # log_activity() issues its own commit, which (with expire_on_commit
    # default) expires every object in the session including `campaign` —
    # refresh it after that, not before, so the attributes FastAPI reads
    # for the response are loaded while the session is still open (see the
    # identical fix on PATCH /employees/{id} and POST /jobs/{id}/qualify).
    # This one happened to work anyway because campaign.id below forced a
    # reload, but that's incidental — don't rely on it.
    log_activity(session, "campaign", campaign.id, "created")
    session.refresh(campaign)
    background_tasks.add_task(run_campaign_search, campaign.id)

    return CampaignCreateResponse(campaign=campaign, is_duplicate=False)


@router.get("", response_model=List[Campaign])
def list_campaigns(session: Session = Depends(get_session)):
    return session.exec(select(Campaign)).all()


@router.get("/{campaign_id}", response_model=Campaign)
def get_campaign(campaign_id: int, session: Session = Depends(get_session)):
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


@router.post("/{campaign_id}/recheck", response_model=Campaign)
def recheck_campaign(campaign_id: int, background_tasks: BackgroundTasks, session: Session = Depends(get_session)):
    """Re-runs the same search_criteria this campaign was created with.
    run_campaign_search() dedupes against jobs already on file (the
    (campaign_id, external_job_id) unique constraint) and flags whatever's
    genuinely new with is_new=True — see its docstring for the full flow."""
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status == "running":
        raise HTTPException(status_code=409, detail="This campaign is already running a search")

    # Set synchronously (not just inside the background task) so the
    # frontend's response to this call already shows "running" and its
    # polling loop engages immediately, rather than racing the task start.
    campaign.status = "running"
    session.add(campaign)
    session.commit()

    log_activity(session, "campaign", campaign_id, "recheck started")
    session.refresh(campaign)
    background_tasks.add_task(run_campaign_search, campaign_id)

    return campaign


@router.delete("/{campaign_id}", status_code=204)
def delete_campaign(campaign_id: int, session: Session = Depends(get_session)):
    """Permanently deletes a campaign and everything under it — jobs,
    qualifications, leads, contacts, employee matches, and emails. None of
    those FKs have an ondelete clause, so children have to go first, deepest
    dependency last.

    These models only declare plain `foreign_key=` columns, not ORM-level
    `Relationship()`s, so SQLAlchemy's unit-of-work has no dependency graph
    to order a single batched flush by — it emitted DELETE FROM campaigns
    before DELETE FROM jobs and hit a FK violation the first time this was
    tried. Each stage below gets its own explicit flush so the DB actually
    sees them in dependency order.
    """
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    job_ids = session.exec(select(Job.id).where(Job.campaign_id == campaign_id)).all()
    lead_ids = session.exec(select(Lead.id).where(Lead.campaign_id == campaign_id)).all()

    if lead_ids:
        for match in session.exec(select(EmployeeMatch).where(EmployeeMatch.lead_id.in_(lead_ids))):
            session.delete(match)
        for email in session.exec(select(Email).where(Email.lead_id.in_(lead_ids))):
            session.delete(email)
        for contact in session.exec(select(Contact).where(Contact.lead_id.in_(lead_ids))):
            session.delete(contact)
        session.flush()

        for lead in session.exec(select(Lead).where(Lead.campaign_id == campaign_id)):
            session.delete(lead)
        session.flush()

    if job_ids:
        for qualification in session.exec(select(Qualification).where(Qualification.job_id.in_(job_ids))):
            session.delete(qualification)
        session.flush()

        for job in session.exec(select(Job).where(Job.campaign_id == campaign_id)):
            session.delete(job)
        session.flush()

    session.delete(campaign)
    session.commit()

    log_activity(
        session,
        "campaign",
        campaign_id,
        f"deleted (with {len(job_ids)} job(s), {len(lead_ids)} lead(s))",
    )


def serialize_jobs(session: Session, jobs: List[Job]) -> List[JobOut]:
    """Shared by the per-campaign and global (GET /jobs) job listings."""
    job_ids = [j.id for j in jobs]
    qualifications_by_job_id = {
        q.job_id: q
        for q in session.exec(select(Qualification).where(Qualification.job_id.in_(job_ids)))
    } if jobs else {}
    lead_ids_by_job_id = {
        lead.job_id: lead.id
        for lead in session.exec(select(Lead).where(Lead.job_id.in_(job_ids)))
    } if jobs else {}

    result = []
    for job in jobs:
        qualification = qualifications_by_job_id.get(job.id)
        # Re-clean at serve time too (not just at ingestion in
        # apify_client.py) so jobs stored before that fix went in still
        # display correctly — idempotent no-op on already-clean text.
        job_fields = job.model_dump()
        job_fields["description"] = clean_html_description(job_fields.get("description"))
        result.append(
            JobOut(
                **job_fields,
                url=(job.raw_data or {}).get("url"),
                qualification_status=qualification.status if qualification else "pending",
                reason=qualification.reason if qualification else None,
                rule_flags=qualification.rule_flags if qualification else None,
                decided_by=qualification.decided_by if qualification else None,
                lead_id=lead_ids_by_job_id.get(job.id),
            )
        )
    return result


@router.get("/{campaign_id}/jobs", response_model=List[JobOut])
def list_campaign_jobs(campaign_id: int, session: Session = Depends(get_session)):
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    jobs = session.exec(select(Job).where(Job.campaign_id == campaign_id)).all()
    return serialize_jobs(session, jobs)
