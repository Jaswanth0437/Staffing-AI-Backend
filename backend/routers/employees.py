from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from backend.activity import log_activity
from backend.database import get_session
from backend.models import Employee
from backend.schemas import EmployeeUpdate

router = APIRouter(prefix="/employees", tags=["employees"])


@router.get("", response_model=List[Employee])
def list_employees(session: Session = Depends(get_session)):
    """The employees table is normally populated via POST
    /admin/sync-employees (Salesforce Employee__c, or the mock seed
    fallback) — PATCH/DELETE below are local manual overrides on top of
    that. Re-running the sync overwrites a matching name's fields back to
    whatever Salesforce has, and re-creates a deleted-but-still-in-Salesforce
    record — sync always wins since it's the source of truth."""
    return session.exec(select(Employee)).all()


@router.patch("/{employee_id}", response_model=Employee)
def update_employee(employee_id: int, payload: EmployeeUpdate, session: Session = Depends(get_session)):
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(employee, field, value)

    session.add(employee)
    session.commit()

    # log_activity() issues its own commit, which (with expire_on_commit
    # default) expires every object in the session including `employee` —
    # refresh it after that, not before, so the attributes FastAPI reads for
    # the response are loaded while the session is still open.
    log_activity(session, "employee", employee_id, f"manually edited: {list(updates.keys())}")
    session.refresh(employee)
    return employee


@router.delete("/{employee_id}", status_code=204)
def delete_employee(employee_id: int, session: Session = Depends(get_session)):
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    session.delete(employee)
    try:
        session.commit()
    except IntegrityError:
        # No ondelete behavior on employee_matches/emails' employee_id FK —
        # an employee already matched or emailed against a lead can't be
        # hard-deleted without also deleting that history.
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="This employee is referenced by an existing match or email and can't be deleted.",
        )
    log_activity(session, "employee", employee_id, "deleted")
