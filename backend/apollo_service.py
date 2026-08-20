"""
Apollo.io contact resolution — pipeline step 4.

People lookups are a two-step Apollo flow, confirmed via live testing:
  1. Search (mixed_people/api_search) — cheap, returns candidates with
     obfuscated names and a `has_email` flag but NOT the real email.
     `/v1/people/search` (the old endpoint this used to call) is now
     deprecated and 422s on every call — that was happening silently here,
     since the broad exception handler swallowed the HTTPError, meaning
     resolve_hr_contact had been returning None unconditionally and every
     lead was silently falling back to the no-email company_level tier.
  2. Match (people/match, by search-result `id`) — unlocks the real email
     for one specific candidate. Costs an Apollo credit per call, so each
     tier below only matches a handful of top candidates rather than every
     search result.

Three tiers, in order — getting a real, usable email is the actual goal
here, not just a name:
  1. resolve_hr_contact() — search + match for an HR/recruiting contact at
     the resolved company domain.
  2. resolve_any_contact_email() — same, no title filter. A wider net for
     when Apollo has no HR/recruiting title at the company but does have
     *someone* there with a real email.
  3. resolve_company_contact() — Organization Search, company-level only.
     This is the guaranteed floor: name/domain/phone/LinkedIn, no email
     (Apollo's org search doesn't return one) — last resort only, when
     Apollo genuinely has no person-level data for the company at all.

All three functions never raise — a failed/unconfigured Apollo call returns
None, and the caller (create-lead) is responsible for the guaranteed floor:
always writing a company_level contacts row using the job's own `company`
field even when Apollo has nothing to add at any tier.
"""

import requests

from backend.config import settings

APOLLO_ORG_SEARCH_URL = "https://api.apollo.io/api/v1/organizations/search"
APOLLO_PEOPLE_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
APOLLO_PEOPLE_MATCH_URL = "https://api.apollo.io/api/v1/people/match"

HR_CONTACT_TITLES = [
    "Human Resources",
    "Talent Acquisition",
    "Recruiting",
    "Recruiter",
    "HR Manager",
    "Talent Acquisition Manager",
]

# How many search results to fetch per tier, and how many of those to spend
# a people/match credit revealing an email for before giving up — matching
# every search result would burn credits fast, so only the top few are
# tried, in the search's own relevance order.
_SEARCH_RESULTS_PER_TIER = 10
_MAX_MATCH_ATTEMPTS = 3


def _search_people(company_domain: str, person_titles: list[str] | None) -> list[dict]:
    payload = {
        "q_organization_domains_list": [company_domain],
        "page": 1,
        "per_page": _SEARCH_RESULTS_PER_TIER,
    }
    if person_titles:
        payload["person_titles"] = person_titles

    response = requests.post(
        APOLLO_PEOPLE_SEARCH_URL,
        headers={"Content-Type": "application/json", "X-Api-Key": settings.APOLLO_API_KEY},
        json=payload,
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("people", [])


def _match_email(person_id: str) -> dict | None:
    """Unlocks one candidate's real email via people/match. Returns
    {name, designation, email, linkedin_url} only when a real (verified or
    guessed, but not missing) email comes back."""
    response = requests.post(
        APOLLO_PEOPLE_MATCH_URL,
        headers={"Content-Type": "application/json", "X-Api-Key": settings.APOLLO_API_KEY},
        json={"id": person_id, "reveal_personal_emails": False},
        timeout=15,
    )
    response.raise_for_status()
    person = response.json().get("person") or {}
    email = person.get("email")
    if not email or "not_unlocked" in email:
        return None
    return {
        "name": person.get("name"),
        "designation": person.get("title"),
        "email": email,
        "linkedin_url": person.get("linkedin_url"),
    }


def _resolve_person_with_email(company_domain: str, person_titles: list[str] | None) -> dict | None:
    if not settings.APOLLO_API_KEY or not company_domain:
        return None

    try:
        candidates = _search_people(company_domain, person_titles)
        for candidate in candidates[:_MAX_MATCH_ATTEMPTS]:
            person_id = candidate.get("id")
            if not person_id:
                continue
            resolved = _match_email(person_id)
            if resolved:
                return resolved
        return None
    except (requests.RequestException, ValueError):
        return None


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


def resolve_hr_contact(company_domain: str) -> dict | None:
    """Looks up an HR/recruiting contact at the given company domain and
    unlocks their real email. Returns {name, designation, email,
    linkedin_url} only when a real email is unlocked for one of the top
    HR-titled candidates."""
    return _resolve_person_with_email(company_domain, HR_CONTACT_TITLES)


def resolve_any_contact_email(company_domain: str) -> dict | None:
    """Broader fallback for when resolve_hr_contact finds no HR/recruiting
    contact with a real email — same search, no title filter, so any
    employee at the company with an unlockable email counts. A real email
    from someone at the company is more useful for outreach than no email
    at all, which is the last-resort company_level tier's only option."""
    return _resolve_person_with_email(company_domain, None)
