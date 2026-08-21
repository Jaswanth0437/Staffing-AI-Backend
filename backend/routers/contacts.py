from typing import List

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from backend.database import get_session
from backend.models import Contact
from backend.schemas import ContactOut

router = APIRouter(prefix="/contacts", tags=["contacts"])


@router.get("", response_model=List[ContactOut])
def list_contacts(session: Session = Depends(get_session)):
    """Every resolved contact across every lead — backs the global Contacts
    tab, which needs a cross-lead view rather than one lead's."""
    return session.exec(select(Contact)).all()
