from fastapi import APIRouter, Depends
from simple_salesforce.exceptions import SalesforceError
from sqlmodel import Session

from backend.activity import log_activity
from backend.database import get_session
from backend.salesforce_service import SalesforceSyncError, sync_employees
from backend.seed_employees import seed_mock_employees

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/sync-employees")
def sync_employees_endpoint(session: Session = Depends(get_session)):
    """Triggers a re-sync of the local employees table from Salesforce
    Employee__c. Falls back to the mock seed (backend/seed_employees.py) if
    Salesforce credentials are missing/invalid, or the Salesforce SDK itself
    errors (e.g. a describe()/query request rejected by the org) — the
    response's "source" field always says which one actually ran, and
    "fallback_reason" carries the exact underlying error either way."""
    try:
        result = sync_employees(session)
    except (SalesforceSyncError, SalesforceError) as exc:
        result = seed_mock_employees(session)
        result["fallback_reason"] = str(exc)

    log_activity(session, "employees", 0, f"sync: {result['source']} ({result['synced']} records)")
    return result
