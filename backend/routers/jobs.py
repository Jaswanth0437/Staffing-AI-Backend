from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from backend.activity import log_activity
from backend.apollo_service import resolve_company_contact
from backend.database import get_session
from backend.models import Contact, Job, Lead, Qualification
from backend.schemas import CreateLeadResponse, ManualQualify

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("/{job_id}/qualify", response_model=Qualification)
def manual_qualify_job(job_id: int, payload: ManualQualify, session: Session = Depends(get_session)):
    if payload.status not in ("qualified", "rejected"):
        raise HTTPException(status_code=400, detail="status must be 'qualified' or 'rejected'")

    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    qualification = session.exec(select(Qualification).where(Qualification.job_id == job_id)).first()
    if qualification is None:
        qualification = Qualification(job_id=job_id, status=payload.status, reason=payload.reason, decided_by="manual")
    else:
        qualification.status = payload.status
        qualification.reason = payload.reason
        qualification.decided_by = "manual"

    session.add(qualification)
    session.commit()
    session.refresh(qualification)

    log_activity(session, "job", job_id, f"manual qualification: {qualification.status}")
    return qualification


@router.post("/{job_id}/create-lead", response_model=CreateLeadResponse)
def create_lead(job_id: int, session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    # Idempotent: a lead already exists for this job, return it (and its
    # existing contact) as-is instead of creating a duplicate and re-spending
    # an Apollo lookup on a repeat call.
    existing_lead = session.exec(select(Lead).where(Lead.job_id == job_id)).first()
    if existing_lead is not None:
        existing_contact = session.exec(select(Contact).where(Contact.lead_id == existing_lead.id)).first()
        return CreateLeadResponse(lead_id=existing_lead.id, status=existing_lead.status, contact=existing_contact)

    qualification = session.exec(select(Qualification).where(Qualification.job_id == job_id)).first()
    if qualification is None or qualification.status != "qualified":
        raise HTTPException(status_code=400, detail="Job must be qualified before a lead can be created")

    lead = Lead(job_id=job.id, campaign_id=job.campaign_id, status="new")
    session.add(lead)
    session.commit()
    session.refresh(lead)
    log_activity(session, "lead", lead.id, "created")

    # M4: Apollo company-level lookup — guaranteed floor. Whether or not Apollo
    # resolves anything, a company_level contact row always exists afterward,
    # using the job's own `company` field as the minimum viable name.
    apollo_result = resolve_company_contact(job.company or "")
    contact = Contact(
        lead_id=lead.id,
        type="company_level",
        name=(apollo_result or {}).get("name") or job.company,
        linkedin_url=(apollo_result or {}).get("linkedin_url"),
        phone=(apollo_result or {}).get("phone"),
        source="apollo" if apollo_result else "job_data_fallback",
    )
    session.add(contact)
    session.commit()
    session.refresh(contact)
    log_activity(session, "lead", lead.id, f"contact resolved via {contact.source}")

    return CreateLeadResponse(lead_id=lead.id, status=lead.status, contact=contact)
