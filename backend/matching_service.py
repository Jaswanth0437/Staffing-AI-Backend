"""
AI employee matching — pipeline step 3 (internal only, never exposed
externally; results are only ever looked at internally before a human
confirms one candidate via POST /leads/{id}/confirm-employee).

Single Gemini call (via backend.ai_service, same wrapper as M2's qualification)
ranks every local employee against the job's requirements and returns the
top-N with a numeric score and reasoning. Same manual-JSON-parse pattern as
backend/qualification.py — backend.ai_service has no structured-output mode.

Two distinct failure/outcome shapes matter here, and callers must not
conflate them:
  - NoEmployeesConfiguredError: the employees table is empty. A server-side
    data problem (nothing to sync'd yet), not a matching failure — callers
    should surface this as a 5xx.
  - MatchingError: the AI call itself errored, returned unparseable JSON, or
    returned entries that don't reference any real employee_id. A genuine
    technical failure — callers should surface this as a 4xx.
  - A legitimate zero-match result (the AI call succeeded, returned valid
    JSON, and correctly determined no employee is a reasonable fit) is NOT
    an error — match_employees_for_job() returns ([], "no_qualifying_employees")
    in that case so callers can return a normal 200.
"""

import json
from typing import Optional

from sqlmodel import Session, select

from backend.ai_service import AIServiceError, generate_text
from backend.config import TOP_N_EMPLOYEE_MATCHES
from backend.models import Employee, Job


class MatchingError(Exception):
    """The AI call failed, was unparseable, or hallucinated invalid employee_ids."""


class NoEmployeesConfiguredError(Exception):
    """The employees table is empty — a server-side data problem."""


def _build_prompt(job: Job, employees: list[Employee]) -> str:
    roster = "\n".join(
        f"- id={e.id}, name={e.name}, role={e.role}, seniority={e.seniority or 'unknown'}, "
        f"skills={', '.join(e.skills) if e.skills else 'none listed'}, "
        f"experience={e.experience_summary or 'none listed'}"
        for e in employees
    )
    return (
        "You are matching internal employees to a client job opening for a staffing/outreach "
        "pitch. Given the job requirements and the employee roster below, rank the employees "
        "by how well their skills/role/seniority/experience fit this specific job. Only include "
        "employees who are a genuinely plausible fit — do not pad the list with weak matches. "
        "If NONE of the employees below are a reasonable fit for this job (e.g. their skills are "
        "in a completely different domain than what the job requires), that is a valid and "
        'expected answer — respond with {"matches": []} rather than forcing a weak match just to '
        "return something.\n\n"
        f"Job title: {job.title or 'unknown'}\n"
        f"Company: {job.company or 'unknown'}\n"
        f"Description: {(job.description or 'unknown')[:3000]}\n\n"
        f"Employee roster:\n{roster}\n\n"
        f"Return the top {TOP_N_EMPLOYEE_MATCHES} matches at most (fewer is fine if fewer are "
        "genuinely plausible fits, and zero is fine if none are). Respond with ONLY a JSON object "
        'and nothing else, in this exact shape: {"matches": [{"employee_id": <int>, "score": '
        '<float 0-1>, "reasoning": "<one to two concise sentences>"}]}, ordered best match first.'
    )


def _parse_ai_response(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    data = json.loads(text)
    matches = data.get("matches")
    if not isinstance(matches, list):
        raise ValueError(f"AI response missing 'matches' list: {data!r}")
    return matches


def match_employees_for_job(session: Session, job: Job) -> tuple[list[dict], Optional[str]]:
    """Runs the AI ranking call. Returns (matches, reason):
      - (matches, None) — one or more genuine matches found.
      - ([], "no_qualifying_employees") — the AI call succeeded and correctly
        found no reasonable fit. Not an error.
    Raises NoEmployeesConfiguredError if the employees table is empty, or
    MatchingError if the AI call fails, returns unparseable output, or
    returns entries that don't reference any real employee_id."""
    employees = session.exec(select(Employee)).all()
    if not employees:
        raise NoEmployeesConfiguredError(
            "No employees available to match against — run POST /admin/sync-employees first."
        )

    try:
        raw_matches = _parse_ai_response(generate_text(_build_prompt(job, employees)))
    except AIServiceError as exc:
        raise MatchingError(f"AI matching call failed: {exc}") from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise MatchingError(f"AI returned an unparseable response: {exc}") from exc

    if not raw_matches:
        # The AI explicitly determined no employee is a reasonable fit — a
        # legitimate outcome, not a technical failure.
        return [], "no_qualifying_employees"

    valid_ids = {e.id for e in employees}
    matches = []
    for m in raw_matches:
        employee_id = m.get("employee_id")
        if employee_id not in valid_ids:
            continue
        matches.append(
            {
                "employee_id": employee_id,
                "score": float(m.get("score", 0)),
                "reasoning": m.get("reasoning", ""),
            }
        )

    if not matches:
        # The AI returned entries, but none referenced a real employee_id —
        # this is a hallucination/parsing problem, not a legitimate zero-match
        # result (which comes back as an empty list, handled above).
        raise MatchingError("AI response contained matches but none referenced a valid employee_id.")

    return matches[:TOP_N_EMPLOYEE_MATCHES], None
