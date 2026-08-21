from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import Session, select

from backend.activity import log_activity
from backend.apify_client import clean_html_description
from backend.campaign_service import run_campaign_search
from backend.database import get_session
from backend.models import Campaign, Job, Lead, Qualification
from backend.schemas import CampaignCreate, JobOut

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


@router.post("", response_model=Campaign)
def create_campaign(
    payload: CampaignCreate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    campaign = Campaign(
        name=payload.name,
        search_criteria=payload.search_criteria.model_dump(),
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

    return campaign


@router.get("", response_model=List[Campaign])
def list_campaigns(session: Session = Depends(get_session)):
    return session.exec(select(Campaign)).all()


@router.get("/{campaign_id}", response_model=Campaign)
def get_campaign(campaign_id: int, session: Session = Depends(get_session)):
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


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
