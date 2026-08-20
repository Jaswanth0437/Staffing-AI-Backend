"""
Job qualification — pipeline step 2.

Two-stage pipeline per job:
  1. Rule-based checks (thresholds from config.py) — deterministic, free, and run
     first. Any rule that fires short-circuits qualification: the job is rejected
     immediately and the AI call is skipped entirely.
  2. AI check (via backend.ai_service, Gemini) — only runs for jobs that pass every
     rule (or have no data for a given rule — see the null-skip comment on the
     rule constants in config.py). Asks the model to judge genuine staffing/
     recruitment fit.

Rule-rejections and AI-rejections both write decided_by="ai" — a rule-based
rejection is still an automated decision, not a manual override. decided_by
only becomes "manual" via POST /jobs/{id}/qualify.

If the AI call fails even after backend.ai_service's built-in retries (bad key,
exhausted rate limit, malformed response), the job is written with
status="pending" and the failure reason, and processing moves on to the next
job — one bad AI call must never take down the whole campaign run.
"""

import json
from typing import Optional

from sqlmodel import Session

from backend.activity import log_activity
from backend.ai_service import AIServiceError, generate_text
from backend.config import APPLICANT_COUNT_CEILING, EMPLOYEE_SIZE_FLOOR, STAFFING_AGENCY_KEYWORDS
from backend.models import Job, Qualification


def check_rules(job: Job) -> tuple[Optional[str], dict]:
    """Runs the deterministic rule checks against a job.

    Returns (rejection_reason, rule_flags). rejection_reason is None if the job
    passes (or has no data for) every rule. rule_flags records what was
    actually checked/skipped, for storage on the qualifications row.
    """
    rule_flags: dict[str, str] = {}

    if job.company_employee_size is not None:
        if job.company_employee_size < EMPLOYEE_SIZE_FLOOR:
            rule_flags["employee_size_floor"] = "failed"
            return (
                f"Company employee size ({job.company_employee_size}) is below the floor of {EMPLOYEE_SIZE_FLOOR}",
                rule_flags,
            )
        rule_flags["employee_size_floor"] = "passed"
    else:
        rule_flags["employee_size_floor"] = "skipped_no_data"

    if job.applicant_count is not None:
        if job.applicant_count > APPLICANT_COUNT_CEILING:
            rule_flags["applicant_count_ceiling"] = "failed"
            return (
                f"Applicant count ({job.applicant_count}) exceeds the ceiling of {APPLICANT_COUNT_CEILING}",
                rule_flags,
            )
        rule_flags["applicant_count_ceiling"] = "passed"
    else:
        rule_flags["applicant_count_ceiling"] = "skipped_no_data"

    company_name = (job.company or "").lower()
    matched_keyword = next((kw for kw in STAFFING_AGENCY_KEYWORDS if kw in company_name), None)
    if matched_keyword:
        rule_flags["staffing_agency_keyword"] = "failed"
        return f'Company name matches staffing-agency keyword "{matched_keyword}"', rule_flags
    rule_flags["staffing_agency_keyword"] = "passed" if company_name else "skipped_no_data"

    return None, rule_flags


def _build_prompt(job: Job) -> str:
    return (
        "You are screening job postings for a staffing/recruitment outreach use case. "
        "Given the job below, decide whether this looks like a genuine, active hiring need "
        "that a staffing agency could realistically pitch itself against — a real company with "
        "a specific role to fill, not a mass-posted template, not obviously an internal-only "
        "role, not something already fully owned by an in-house recruiting pipeline that would "
        "reject external staffing vendors.\n\n"
        f"Title: {job.title or 'unknown'}\n"
        f"Company: {job.company or 'unknown'}\n"
        f"Location: {job.location or 'unknown'}\n"
        f"Description: {(job.description or 'unknown')[:3000]}\n\n"
        "Respond with ONLY a JSON object and nothing else: "
        '{"status": "qualified" or "rejected", "reason": "<one concise sentence>"}'
    )


def _parse_ai_response(text: str) -> tuple[str, str]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    data = json.loads(text)
    status = data.get("status")
    if status not in ("qualified", "rejected"):
        raise ValueError(f"AI returned an unrecognized status: {status!r}")
    return status, data.get("reason", "")


def qualify_job(session: Session, job: Job) -> Qualification:
    """Runs the full rule+AI pipeline for one job and writes its qualifications
    row. Never raises — an AI failure is caught and recorded as status
    'pending' with the failure reason so the caller's loop can keep going."""
    rejection_reason, rule_flags = check_rules(job)

    if rejection_reason:
        status, reason = "rejected", rejection_reason
    else:
        try:
            status, reason = _parse_ai_response(generate_text(_build_prompt(job)))
        except AIServiceError as exc:
            status, reason = "pending", f"AI qualification failed after retries: {exc}"
        except (ValueError, json.JSONDecodeError) as exc:
            status, reason = "pending", f"AI returned an unparseable response: {exc}"

    qualification = Qualification(
        job_id=job.id,
        status=status,
        reason=reason,
        rule_flags=rule_flags,
        decided_by="ai",
    )
    session.add(qualification)
    session.commit()
    session.refresh(qualification)

    log_activity(session, "job", job.id, f"qualification: {qualification.status} ({qualification.reason})")
    return qualification
