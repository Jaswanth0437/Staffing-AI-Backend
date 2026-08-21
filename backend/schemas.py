from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

from backend.models import Campaign


class SearchCriteria(BaseModel):
    job_role: Optional[str] = None
    location: Optional[str] = None
    country: Optional[str] = None
    experience_level: Optional[str] = None
    employment_type: Optional[str] = None
    work_mode: Optional[str] = None
    company: Optional[str] = None
    posting_timeframe: Optional[str] = None
    # "linkedin" | "dice" | "both" — which Apify actor(s) campaign_service's
    # run_campaign_search() calls. Defaults to "linkedin" so existing
    # campaigns/callers created before this field existed keep behaving
    # exactly as they do today.
    job_source: Optional[str] = "linkedin"


class CampaignCreate(BaseModel):
    name: str
    search_criteria: SearchCriteria


class CampaignCreateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, arbitrary_types_allowed=True)

    campaign: Campaign
    is_duplicate: bool = False  # True if `campaign` is a pre-existing match, not a new row


class JobOut(BaseModel):
    id: int
    campaign_id: int
    external_job_id: str
    title: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    posted_date: Optional[datetime] = None
    applicant_count: Optional[int] = None
    company_employee_size: Optional[int] = None
    source: Optional[str] = None
    work_mode_signal: Optional[str] = None
    url: Optional[str] = None  # original LinkedIn/Dice posting URL, pulled from raw_data
    created_at: datetime
    is_new: bool = False  # set on jobs inserted by the most recent recheck run
    qualification_status: str  # pending | qualified | rejected
    reason: Optional[str] = None
    rule_flags: Optional[dict] = None  # per-criterion passed/failed/skipped_no_data breakdown
    decided_by: Optional[str] = None  # ai | manual — None if not qualified yet
    lead_id: Optional[int] = None  # set once a lead has been created from this job


class ManualQualify(BaseModel):
    status: str  # qualified | rejected
    reason: Optional[str] = None


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    lead_id: int
    type: str
    name: Optional[str] = None
    designation: Optional[str] = None
    linkedin_url: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    source: Optional[str] = None


class LeadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    campaign_id: int
    status: str
    created_at: datetime


class CreateLeadResponse(BaseModel):
    lead_id: int
    status: str
    contact: ContactOut


class ConfirmEmployee(BaseModel):
    match_id: int


class EmailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    lead_id: int
    employee_id: Optional[int] = None
    sender: Optional[str] = None
    recipient: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    status: str
    sent_at: Optional[datetime] = None


class EmailUpdate(BaseModel):
    subject: Optional[str] = None
    body: Optional[str] = None
    sender: Optional[str] = None
    recipient: Optional[str] = None


class EmployeeMatchOut(BaseModel):
    id: int
    lead_id: int
    employee_id: int
    employee_name: str
    employee_role: str
    match_score: float
    ai_reasoning: Optional[str] = None
    confirmed: bool


class MatchEmployeesResponse(BaseModel):
    employee_matches: list[EmployeeMatchOut]
    reason: Optional[str] = None  # e.g. "no_qualifying_employees" when employee_matches is empty


class EmployeeUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    skills: Optional[list[str]] = None
    experience_summary: Optional[str] = None
    seniority: Optional[str] = None
