from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import Session, select

from backend.activity import log_activity
from backend.campaign_service import run_campaign_search
from backend.database import get_session
from backend.models import Campaign, Job, Qualification
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
    session.refresh(campaign)

    log_activity(session, "campaign", campaign.id, "created")
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


@router.get("/{campaign_id}/jobs", response_model=List[JobOut])
def list_campaign_jobs(campaign_id: int, session: Session = Depends(get_session)):
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    jobs = session.exec(select(Job).where(Job.campaign_id == campaign_id)).all()
    qualifications_by_job_id = {
        q.job_id: q
        for q in session.exec(select(Qualification).where(Qualification.job_id.in_([j.id for j in jobs])))
    } if jobs else {}

    result = []
    for job in jobs:
        qualification = qualifications_by_job_id.get(job.id)
        result.append(
            JobOut(
                **job.model_dump(),
                qualification_status=qualification.status if qualification else "pending",
                reason=qualification.reason if qualification else None,
            )
        )
    return result
