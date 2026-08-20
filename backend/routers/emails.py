from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from backend.activity import log_activity
from backend.config import settings
from backend.database import get_session
from backend.email_service import EmailGenerationError, generate_and_persist_email
from backend.graph_email_service import GraphEmailError, send_email
from backend.models import Email, Lead, utcnow
from backend.schemas import EmailOut, EmailUpdate

router = APIRouter(prefix="/emails", tags=["emails"])


@router.get("/{email_id}", response_model=EmailOut)
def get_email(email_id: int, session: Session = Depends(get_session)):
    email = session.get(Email, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="Email not found")
    return email


@router.put("/{email_id}", response_model=EmailOut)
def update_email(email_id: int, payload: EmailUpdate, session: Session = Depends(get_session)):
    email = session.get(Email, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="Email not found")

    if payload.subject is not None:
        email.subject = payload.subject
    if payload.body is not None:
        email.body = payload.body
    if payload.sender is not None:
        email.sender = payload.sender
    if payload.recipient is not None:
        email.recipient = payload.recipient

    session.add(email)
    session.commit()
    session.refresh(email)

    log_activity(session, "email", email.id, "manually edited")
    return email


@router.post("/{email_id}/regenerate", response_model=EmailOut)
def regenerate_email(email_id: int, session: Session = Depends(get_session)):
    email = session.get(Email, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="Email not found")

    lead = session.get(Lead, email.lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead for this email not found")

    try:
        email = generate_and_persist_email(session, lead)
    except EmailGenerationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    log_activity(session, "email", email.id, "regenerated")
    return email


@router.post("/{email_id}/send", response_model=EmailOut)
def send_email_endpoint(email_id: int, session: Session = Depends(get_session)):
    email = session.get(Email, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="Email not found")

    if not email.recipient:
        raise HTTPException(status_code=422, detail="Email has no recipient set — use PUT /emails/{id} first")

    try:
        send_email(
            sender=settings.GRAPH_SENDER_EMAIL,
            recipient=email.recipient,
            subject=email.subject,
            body=email.body,
        )
    except GraphEmailError as exc:
        email.status = "failed"
        session.add(email)
        session.commit()
        session.refresh(email)
        log_activity(session, "email", email.id, f"send failed: {exc}")
        raise HTTPException(status_code=502, detail=f"Failed to send email via Microsoft Graph: {exc}")

    email.status = "sent"
    email.sent_at = utcnow()
    session.add(email)
    session.commit()
    session.refresh(email)

    log_activity(session, "email", email.id, "sent")
    return email
