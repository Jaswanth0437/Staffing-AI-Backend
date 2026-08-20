from sqlmodel import Session

from backend.models import ActivityLog


def log_activity(session: Session, entity_type: str, entity_id: int, action: str) -> ActivityLog:
    entry = ActivityLog(entity_type=entity_type, entity_id=entity_id, action=action)
    session.add(entry)
    session.commit()
    session.refresh(entry)
    return entry
