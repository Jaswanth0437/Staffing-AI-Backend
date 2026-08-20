from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from backend.activity import log_activity
from backend.database import get_session
from backend.email_service import EmailGenerationError, generate_and_persist_email
from backend.matching_service import MatchingError, NoEmployeesConfiguredError, match_employees_for_job
from backend.models import Employee, EmployeeMatch, Job, Lead
from backend.schemas import ConfirmEmployee, EmailOut, EmployeeMatchOut, LeadOut, MatchEmployeesResponse

router = APIRouter(prefix="/leads", tags=["leads"])


@router.get("/{lead_id}", response_model=LeadOut)
def get_lead(lead_id: int, session: Session = Depends(get_session)):
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


def _matches_to_out(session: Session, lead_id: int, matches: List[EmployeeMatch]) -> List[EmployeeMatchOut]:
    employees_by_id = {e.id: e for e in session.exec(select(Employee)).all()}
    result = []
    for m in matches:
        employee = employees_by_id.get(m.employee_id)
        result.append(
            EmployeeMatchOut(
                id=m.id,
                lead_id=lead_id,
                employee_id=m.employee_id,
                employee_name=employee.name if employee else "unknown",
                employee_role=employee.role if employee else "unknown",
                match_score=m.match_score,
                ai_reasoning=m.ai_reasoning,
                confirmed=m.confirmed,
            )
        )
    return result


@router.post("/{lead_id}/match-employees", response_model=MatchEmployeesResponse)
def match_employees(lead_id: int, session: Session = Depends(get_session)):
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")

    job = session.get(Job, lead.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job for this lead not found")

    try:
        ranked, reason = match_employees_for_job(session, job)
    except NoEmployeesConfiguredError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except MatchingError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # Re-running match-employees replaces the prior ranked list for this lead,
    # including replacing a prior legitimate zero-match result with a fresh one.
    existing = session.exec(select(EmployeeMatch).where(EmployeeMatch.lead_id == lead_id)).all()
    for m in existing:
        session.delete(m)
    session.commit()

    created = []
    for entry in ranked:
        match = EmployeeMatch(
            lead_id=lead_id,
            employee_id=entry["employee_id"],
            match_score=entry["score"],
            ai_reasoning=entry["reasoning"],
            confirmed=False,
        )
        session.add(match)
        created.append(match)
    session.commit()
    for m in created:
        session.refresh(m)

    log_activity(session, "lead", lead_id, f"matched {len(created)} employees" + (f" ({reason})" if reason else ""))
    return MatchEmployeesResponse(employee_matches=_matches_to_out(session, lead_id, created), reason=reason)


@router.get("/{lead_id}/matches", response_model=List[EmployeeMatchOut])
def get_matches(lead_id: int, session: Session = Depends(get_session)):
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")

    matches = session.exec(
        select(EmployeeMatch).where(EmployeeMatch.lead_id == lead_id).order_by(EmployeeMatch.match_score.desc())
    ).all()
    return _matches_to_out(session, lead_id, matches)


@router.post("/{lead_id}/confirm-employee", response_model=EmployeeMatchOut)
def confirm_employee(lead_id: int, payload: ConfirmEmployee, session: Session = Depends(get_session)):
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")

    match_id = payload.match_id
    match = session.get(EmployeeMatch, match_id)
    if match is None or match.lead_id != lead_id:
        raise HTTPException(status_code=404, detail="Employee match not found for this lead")

    other_matches = session.exec(
        select(EmployeeMatch).where(EmployeeMatch.lead_id == lead_id, EmployeeMatch.id != match_id)
    ).all()
    for m in other_matches:
        if m.confirmed:
            m.confirmed = False
            session.add(m)

    match.confirmed = True
    session.add(match)
    session.commit()
    session.refresh(match)

    log_activity(session, "lead", lead_id, f"confirmed employee_match {match.id}")
    return _matches_to_out(session, lead_id, [match])[0]


@router.post("/{lead_id}/generate-email", response_model=EmailOut)
def generate_email(lead_id: int, session: Session = Depends(get_session)):
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")

    try:
        email = generate_and_persist_email(session, lead)
    except EmailGenerationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    log_activity(session, "lead", lead_id, f"generated email (id={email.id})")
    return email
