from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from backend.activity import log_activity
from backend.apollo_service import resolve_any_contact_email, resolve_company_contact, resolve_hr_contact
from backend.database import get_session
from backend.models import Contact, Job, Lead, Qualification
from backend.routers.campaigns import serialize_jobs
from backend.schemas import CreateLeadResponse, JobOut, ManualQualify

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=List[JobOut])
def list_all_jobs(session: Session = Depends(get_session)):
    """Every job across every campaign — backs the Companies/Dashboard
    tabs, which need a cross-campaign view rather than one campaign's."""
    jobs = session.exec(select(Job)).all()
    return serialize_jobs(session, jobs)


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

    # log_activity() issues its own commit, which (with expire_on_commit
    # default) expires every object in the session including
    # `qualification` — refresh it after that, not before, so the
    # attributes FastAPI reads for the response are loaded while the
    # session is still open (see the identical fix on PATCH /employees/{id}).
    log_activity(session, "job", job_id, f"manual qualification: {qualification.status}")
    session.refresh(qualification)
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

    # Cascade: company-level org lookup first (to get a domain to search
    # against), then try to upgrade to a real contact with an actually-
    # unlocked email — HR/recruiting titles first, then any employee at the
    # company, since getting a usable email is the actual goal, not the
    # title. Whichever tier resolves, a contacts row always exists
    # afterward — company_level using the job's own `company` field is the
    # guaranteed floor when Apollo has no email at any tier.
    apollo_result = resolve_company_contact(job.company or "")
    domain = (apollo_result or {}).get("domain") or ""
    person_contact = resolve_hr_contact(domain)
    contact_type = "hr_contact"
    if person_contact is None:
        person_contact = resolve_any_contact_email(domain)
        contact_type = "employee_contact"

    if person_contact:
        contact = Contact(
            lead_id=lead.id,
            type=contact_type,
            name=person_contact.get("name"),
            designation=person_contact.get("designation"),
            email=person_contact.get("email"),
            linkedin_url=person_contact.get("linkedin_url"),
            source="apollo",
        )
    else:
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
    log_activity(session, "lead", lead.id, f"contact resolved via {contact.source} ({contact.type})")

    return CreateLeadResponse(lead_id=lead.id, status=lead.status, contact=contact)
