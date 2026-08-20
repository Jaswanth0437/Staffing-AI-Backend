"""
Salesforce Resource__c sync — pipeline step 3 (employee data source).

Auth: OAuth2 client credentials flow against a connected app (SF_CLIENT_ID /
SF_CLIENT_SECRET / SF_LOGIN_URL) — no username/password/security-token
involved. The token response also carries `instance_url`, which is required
for all subsequent REST calls (it points at the actual org/sandbox instance,
not the login domain).

Field mapping is a hand-confirmed static table (CONFIRMED_FIELD_MAPPING), not
keyword-guessed — this org's Resource__c has 150+ fields with heavily
overlapping labels (multiple fields contain "Experience", for instance), so
automatic keyword matching would have silently picked a wrong field for
several columns. Every mapping below was checked against a live describe()
call plus real record data before being hardcoded:
  - name -> Name (confirmed, populated on all records)
  - role -> Role__c (exact semantic match, but only ~15% of records have it
    set in this sandbox — real field, sparse data)
  - skills -> Skill__c (multipicklist; Salesforce stores it as a
    semicolon-delimited string, not an array or comma-delimited)
  - experience_summary -> About__c (no dedicated bio/summary field exists;
    this free-text textarea is the closest fit, but is populated on only
    ~2% of records in this sandbox)
  - seniority -> Experience_Bucket__c (no dedicated seniority field exists;
    this picklist is the closest fit, though every record currently shares
    the same value in this sandbox)

discover_field_mapping() still calls describe() at sync time, but only to
verify the confirmed API names still exist on the object (a schema-drift
guard) — not to re-derive the mapping.

Upserts into the local `employees` table. Matching reads from that local
table — this module is only ever invoked on-demand via
POST /admin/sync-employees, never per-request.

If SF_* credentials are missing/invalid, sync_employees() raises
SalesforceSyncError. The caller (the admin route) is responsible for deciding
whether to fall back to the mock seed (see backend/seed_employees.py).
"""

import time
from typing import Optional

import requests
from simple_salesforce import Salesforce
from sqlmodel import Session, select

from backend.config import SALESFORCE_EMPLOYEE_OBJECT, settings
from backend.models import Employee

TOKEN_URL = "{login_url}/services/oauth2/token"

# Hand-confirmed against a live describe() + sample Resource__c records — see
# the module docstring for the per-field rationale and data-quality caveats.
CONFIRMED_FIELD_MAPPING = {
    "name": "Name",
    "role": "Role__c",
    "skills": "Skill__c",
    "experience_summary": "About__c",
    "seniority": "Experience_Bucket__c",
}

# Refresh a bit before actual expiry to avoid racing a token that dies mid-request.
TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS = 60

_cached_access_token: Optional[str] = None
_cached_instance_url: Optional[str] = None
_cached_token_expires_at: float = 0.0


class SalesforceSyncError(Exception):
    pass


def get_access_token() -> tuple[str, str]:
    """Returns (access_token, instance_url), cached until near expiry.
    Salesforce's client-credentials token response doesn't include
    `expires_in`, so we conservatively cache for 15 minutes and refetch after
    that rather than assuming a lifetime.
    """
    global _cached_access_token, _cached_instance_url, _cached_token_expires_at

    if _cached_access_token and time.time() < _cached_token_expires_at:
        return _cached_access_token, _cached_instance_url

    if not settings.SF_CLIENT_ID or not settings.SF_CLIENT_SECRET or not settings.SF_LOGIN_URL:
        raise SalesforceSyncError(
            "Missing Salesforce credentials in .env: "
            + ", ".join(
                name
                for name, value in [
                    ("SF_CLIENT_ID", settings.SF_CLIENT_ID),
                    ("SF_CLIENT_SECRET", settings.SF_CLIENT_SECRET),
                    ("SF_LOGIN_URL", settings.SF_LOGIN_URL),
                ]
                if not value
            )
        )

    try:
        response = requests.post(
            TOKEN_URL.format(login_url=settings.SF_LOGIN_URL.rstrip("/")),
            data={
                "grant_type": "client_credentials",
                "client_id": settings.SF_CLIENT_ID,
                "client_secret": settings.SF_CLIENT_SECRET,
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        raise SalesforceSyncError(f"Network error fetching Salesforce token: {exc}") from exc

    if response.status_code != 200:
        raise SalesforceSyncError(f"Salesforce token request failed: {response.status_code} {response.text}")

    data = response.json()
    access_token = data.get("access_token")
    instance_url = data.get("instance_url")
    if not access_token or not instance_url:
        raise SalesforceSyncError(f"Salesforce token response missing access_token/instance_url: {data!r}")

    _cached_access_token = access_token
    _cached_instance_url = instance_url
    _cached_token_expires_at = time.time() + (15 * 60) - TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS
    return access_token, instance_url


def _connect() -> Salesforce:
    access_token, instance_url = get_access_token()
    return Salesforce(session_id=access_token, instance_url=instance_url)


def discover_field_mapping(sf: Salesforce) -> dict[str, str]:
    """Verifies CONFIRMED_FIELD_MAPPING's field names still exist on
    SALESFORCE_EMPLOYEE_OBJECT (a schema-drift guard against a future field
    rename/deletion) and returns the mapping. Does not re-derive it."""
    describe = getattr(sf, SALESFORCE_EMPLOYEE_OBJECT).describe()
    available_api_names = {field["name"] for field in describe["fields"]}

    missing = [api_name for api_name in CONFIRMED_FIELD_MAPPING.values() if api_name not in available_api_names]
    if missing:
        raise SalesforceSyncError(
            f"Confirmed field mapping references fields no longer present on "
            f"{SALESFORCE_EMPLOYEE_OBJECT}: {missing}"
        )
    return dict(CONFIRMED_FIELD_MAPPING)


def _coerce_skills(raw_value) -> list:
    if raw_value is None:
        return []
    if isinstance(raw_value, list):
        return raw_value
    # Salesforce multi-select picklists come back as a semicolon-delimited string.
    return [s.strip() for s in str(raw_value).split(";") if s.strip()]


def sync_employees(session: Session) -> dict:
    """Pulls all Resource__c records from Salesforce and upserts them into the
    local employees table, keyed by name (Resource__c has no natural local FK).
    Returns a summary dict: {"source": "salesforce", "synced": N, "field_mapping": {...}}.
    """
    sf = _connect()
    mapping = discover_field_mapping(sf)

    soql_fields = ["Id"] + [api_name for api_name in mapping.values()]
    query = f"SELECT {', '.join(soql_fields)} FROM {SALESFORCE_EMPLOYEE_OBJECT}"
    records = sf.query_all(query)["records"]

    existing_by_name = {e.name: e for e in session.exec(select(Employee)).all()}

    synced = 0
    for record in records:
        name = record.get(mapping["name"]) or "Unknown"
        employee = existing_by_name.get(name, Employee(name=name, role=""))
        employee.name = name
        employee.role = record.get(mapping["role"]) or ""
        employee.skills = _coerce_skills(record.get(mapping["skills"]))
        employee.experience_summary = record.get(mapping["experience_summary"])
        employee.seniority = record.get(mapping["seniority"])
        session.add(employee)
        synced += 1

    session.commit()
    return {"source": "salesforce", "synced": synced, "field_mapping": mapping}
