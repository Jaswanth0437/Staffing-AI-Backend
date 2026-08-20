"""
Outreach email generation — pipeline step 5.

This is a company capability pitch, never a candidate pitch: the generated
email must never name, describe, or otherwise identify the confirmed
employee. Only a de-identified list of overlapping skill/tech terms (the
intersection of the job's requirement terms and the employee's skills array)
ever reaches the prompt — the employee's name, role, seniority, and
experience_summary are deliberately never passed to the AI call at all, so
there's nothing for the model to leak.
"""

import json
import re

from sqlmodel import Session, select

from backend.ai_service import AIServiceError, generate_text
from backend.config import COMPANY_NAME, COMPANY_PITCH_TONE, COMPANY_SERVICES
from backend.models import Contact, Email, Employee, EmployeeMatch, Job, Lead


class EmailGenerationError(Exception):
    pass


_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+.#-]*")


def _tokenize(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def extract_overlapping_skills(job: Job, employee: Employee) -> list[str]:
    """Intersects the job's title/description text with the employee's skills
    array. A skill counts as overlapping if its normalized form appears as a
    token (or token sequence, for multi-word skills like "Machine Learning")
    somewhere in the job text. Returns the original skill strings (as written
    in the employee's skills array) that matched, preserving their order."""
    job_text = f"{job.title or ''} {job.description or ''}".lower()
    job_tokens = _tokenize(job_text)

    overlapping = []
    for skill in employee.skills or []:
        skill_lower = skill.lower()
        skill_tokens = _tokenize(skill_lower)
        if not skill_tokens:
            continue
        if skill_tokens.issubset(job_tokens) or skill_lower in job_text:
            overlapping.append(skill)
    return overlapping


def _build_prompt(job: Job, overlapping_skills: list[str]) -> str:
    skills_text = ", ".join(overlapping_skills) if overlapping_skills else "general software engineering capability"
    services_text = ", ".join(COMPANY_SERVICES)
    return (
        f"You are writing a cold outreach email on behalf of {COMPANY_NAME}, a firm offering "
        f"{services_text}. Tone: {COMPANY_PITCH_TONE}.\n\n"
        "This is a COMPANY CAPABILITY PITCH, not a candidate pitch. Do not mention, name, "
        "describe, or hint at any specific individual person, their seniority, their years of "
        "experience, or any identifying detail about anyone on the team. Only reference "
        "generalized skill/technology terms as capabilities the company has, e.g. \"our team has "
        "hands-on experience with X\" — never \"we have someone who...\" or any phrasing implying "
        "a specific person.\n\n"
        f"The email is about this specific job opening the company noticed:\n"
        f"Title: {job.title or 'unknown'}\n"
        f"Company: {job.company or 'unknown'}\n\n"
        f"Relevant capability areas to reference (generalized skill terms only): {skills_text}\n\n"
        "Structure the email body as:\n"
        "1. Brief sender intro (who we are, one sentence).\n"
        "2. Reference to the specific job/opening noticed at their company.\n"
        "3. A capability statement using the relevant skill terms above — framed as company "
        "capability, not a person.\n"
        "4. An offer to share relevant profiles/team members if they're open to external support.\n"
        "5. A soft call-to-action for a quick call or to share availability.\n\n"
        "Respond with ONLY a JSON object and nothing else: "
        '{"subject": "<email subject line>", "body": "<full email body, plain text>"}'
    )


def _parse_ai_response(text: str) -> tuple[str, str]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    data = json.loads(text)
    subject, body = data.get("subject"), data.get("body")
    if not subject or not body:
        raise ValueError(f"AI response missing subject/body: {data!r}")
    return subject, body


def generate_email_content(job: Job, employee: Employee) -> tuple[str, str, list[str]]:
    """Returns (subject, body, overlapping_skills). Raises EmailGenerationError
    on AI failure/unparseable response."""
    overlapping_skills = extract_overlapping_skills(job, employee)
    try:
        subject, body = _parse_ai_response(generate_text(_build_prompt(job, overlapping_skills)))
    except AIServiceError as exc:
        raise EmailGenerationError(f"AI email generation failed: {exc}") from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise EmailGenerationError(f"AI returned an unparseable response: {exc}") from exc
    return subject, body, overlapping_skills


def generate_and_persist_email(session: Session, lead: Lead) -> Email:
    """Resolves the lead's job + confirmed employee match, generates the
    email content, and creates/overwrites the lead's single `emails` row
    (draft). Raises EmailGenerationError if there's no confirmed match yet or
    the AI call fails."""
    job = session.get(Job, lead.job_id)
    if job is None:
        raise EmailGenerationError("Job for this lead not found")

    confirmed_match = session.exec(
        select(EmployeeMatch).where(EmployeeMatch.lead_id == lead.id, EmployeeMatch.confirmed == True)  # noqa: E712
    ).first()
    if confirmed_match is None:
        raise EmailGenerationError("No confirmed employee match for this lead — call POST /leads/{id}/confirm-employee first")

    employee = session.get(Employee, confirmed_match.employee_id)
    if employee is None:
        raise EmailGenerationError("Confirmed employee record not found")

    subject, body, _overlapping_skills = generate_email_content(job, employee)

    contact = session.exec(select(Contact).where(Contact.lead_id == lead.id)).first()

    email = session.exec(select(Email).where(Email.lead_id == lead.id)).first()
    if email is None:
        email = Email(lead_id=lead.id)

    email.employee_id = employee.id
    email.subject = subject
    email.body = body
    email.sender = COMPANY_NAME
    email.recipient = (contact.email if contact else None) or email.recipient
    email.status = "draft"
    email.sent_at = None

    session.add(email)
    session.commit()
    session.refresh(email)
    return email
