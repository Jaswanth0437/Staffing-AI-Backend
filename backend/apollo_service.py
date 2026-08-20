"""
Apollo.io company-level contact resolution — pipeline step 4, company-level
lookup only (per BUILD_PLAN.md M4 hackathon cut).

resolve_company_contact() never raises — a failed/unconfigured Apollo call
returns None, and the caller (create-lead) is responsible for the guaranteed
floor: always writing a company_level contacts row using the job's own
`company` field even when Apollo has nothing to add.
"""

import requests

from backend.config import settings

APOLLO_ORG_SEARCH_URL = "https://api.apollo.io/api/v1/organizations/search"


def resolve_company_contact(company_name: str) -> dict | None:
    """Looks up a company on Apollo. Returns a dict of enrichment fields
    (name, domain, industry, phone) on success, or None if Apollo is
    unconfigured, errors, or has no match."""
    if not settings.APOLLO_API_KEY or not company_name:
        return None

    try:
        response = requests.post(
            APOLLO_ORG_SEARCH_URL,
            headers={"Content-Type": "application/json", "X-Api-Key": settings.APOLLO_API_KEY},
            json={"q_organization_name": company_name, "page": 1, "per_page": 1},
            timeout=15,
        )
        response.raise_for_status()
        organizations = response.json().get("organizations", [])
        if not organizations:
            return None
        org = organizations[0]
        return {
            "name": org.get("name") or company_name,
            "domain": org.get("primary_domain"),
            "phone": org.get("phone"),
            "linkedin_url": org.get("linkedin_url"),
        }
    except (requests.RequestException, ValueError):
        return None
