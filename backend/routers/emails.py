from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from backend.activity import log_activity
from backend.config import CEO_NOTIFICATION_EMAIL, settings
from backend.database import get_session
from backend.email_service import EmailGenerationError, generate_and_persist_email
from backend.graph_email_service import GraphEmailError, send_email
from backend.models import Email, Employee, EmployeeMatch, Job, Lead, utcnow
from backend.schemas import EmailOut, EmailUpdate

router = APIRouter(prefix="/emails", tags=["emails"])


def _build_ceo_notification(session: Session, email: Email, lead: Lead) -> tuple[str, str]:
    """Internal-only notification, unlike the lead-facing email — this one
    is allowed to name the actual matched employee."""
    job = session.get(Job, lead.job_id)
    confirmed_match = session.exec(
        select(EmployeeMatch).where(EmployeeMatch.lead_id == lead.id, EmployeeMatch.confirmed == True)  # noqa: E712
    ).first()
    employee = session.get(Employee, confirmed_match.employee_id) if confirmed_match else None

    subject = f"Outreach sent: {job.title if job else 'a lead'} at {job.company if job else 'unknown company'}"

    if employee and confirmed_match:
        match_block = (
            f"Matched employee: {employee.name} ({employee.role})\n"
            f"Match score: {round(confirmed_match.match_score * 100)}%\n"
            f"Match reasoning: {confirmed_match.ai_reasoning or 'n/a'}"
        )
    else:
        match_block = "No confirmed employee match on record for this lead."

    body = (
        f"{email.sender} just sent outreach to {email.recipient} "
        f"for {job.title if job else 'a role'} at {job.company if job else 'this company'}.\n\n"
        f"--- Employee Match ---\n{match_block}\n\n"
        f"--- Message Sent ---\n"
        f"Subject: {email.subject}\n\n"
        f"{email.body}"
    )
    return subject, body


@router.get("", response_model=List[EmailOut])
def list_emails(session: Session = Depends(get_session)):
    return session.exec(select(Email).order_by(Email.id.desc())).all()


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

    # `email.sender` is the "From" field as edited in the UI — normally the
    # logged-in user's own address, so outreach sends as them, not a fixed
    # service account. Only falls back to the configured default if that
    # field isn't a real address (e.g. an older row from before this).
    sender = email.sender if email.sender and "@" in email.sender else settings.GRAPH_SENDER_EMAIL

    try:
        send_email(
            sender=sender,
            recipient=email.recipient,
            subject=email.subject,
            body=email.body,
            include_signature_image=True,
        )
    except GraphEmailError as exc:
        email.status = "failed"
        session.add(email)
        session.commit()
        session.refresh(email)
        log_activity(session, "email", email.id, f"send failed: {exc}")
        raise HTTPException(status_code=502, detail=f"Failed to send email via Microsoft Graph: {exc}")

    email.sender = sender
    email.status = "sent"
    email.sent_at = utcnow()
    session.add(email)

    lead = session.get(Lead, email.lead_id)
    if lead is not None:
        lead.status = "contacted"
        session.add(lead)

    session.commit()
    session.refresh(email)

    log_activity(session, "email", email.id, "sent")

    # Best-effort internal notification — a failure here must never turn a
    # successful outreach send into an error response for the caller.
    if lead is not None:
        try:
            notif_subject, notif_body = _build_ceo_notification(session, email, lead)
            send_email(sender=sender, recipient=CEO_NOTIFICATION_EMAIL, subject=notif_subject, body=notif_body)
            log_activity(session, "email", email.id, f"CEO notification sent to {CEO_NOTIFICATION_EMAIL}")
        except GraphEmailError as exc:
            log_activity(session, "email", email.id, f"CEO notification failed: {exc}")

    return email
