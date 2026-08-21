from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, UniqueConstraint
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Campaign(SQLModel, table=True):
    __tablename__ = "campaigns"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    search_criteria: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)
    last_checked_at: Optional[datetime] = None
    status: str = Field(default="pending")  # pending | running | completed | failed
    # Outcome of the most recent search/recheck run — lets the UI explain an
    # empty jobs list (e.g. "20 fetched, all filtered out") instead of
    # looking identical to "nothing was ever found". Shape:
    # {"fetched": int, "inserted": int, "filtered_out": int,
    #  "reasons": {"employment_type": int, "work_mode": int, "company": int},
    #  "errors": {"<source>": "<error message>"}}
    # Optional/absent keys mean "none of that kind" — see campaign_service.py.
    last_run_summary: Optional[dict] = Field(default=None, sa_column=Column(JSON))


class Job(SQLModel, table=True):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("campaign_id", "external_job_id", name="uq_campaign_external_job_id"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaigns.id")
    external_job_id: str
    title: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    posted_date: Optional[datetime] = None
    applicant_count: Optional[int] = None
    company_employee_size: Optional[int] = None
    source: Optional[str] = None  # which Apify actor produced this row, e.g. "linkedin" | "dice"
    # Raw, unnormalized work-mode text as reported by the source (e.g. Dice's "On-Site" /
    # "Remote" / "Hybrid" workSetting). Not every source provides this — null if absent.
    # No business-rule filtering on this yet; it's just captured for the future work_mode rule.
    work_mode_signal: Optional[str] = None
    raw_data: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)
    # True only for jobs inserted by the most recent recheck run — cleared
    # (on every other job in the campaign) at the start of each new run, so
    # only the latest batch is ever flagged. Lets the UI show a "New" tag and
    # sort the latest finds to the top without needing to compare timestamps.
    is_new: bool = Field(default=False)


class Qualification(SQLModel, table=True):
    __tablename__ = "qualifications"

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="jobs.id", unique=True)
    status: str  # qualified | rejected | pending (AI call failed after retries — reason holds the error)
    reason: Optional[str] = None
    rule_flags: dict = Field(default_factory=dict, sa_column=Column(JSON))
    decided_by: str = Field(default="ai")  # ai | manual
    created_at: datetime = Field(default_factory=utcnow)


class Lead(SQLModel, table=True):
    __tablename__ = "leads"

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="jobs.id")
    campaign_id: int = Field(foreign_key="campaigns.id")
    status: str = Field(default="new")  # new | contacted | replied | closed
    created_at: datetime = Field(default_factory=utcnow)


class Contact(SQLModel, table=True):
    __tablename__ = "contacts"

    id: Optional[int] = Field(default=None, primary_key=True)
    lead_id: int = Field(foreign_key="leads.id")
    type: str  # job_poster | hr_contact | company_level
    name: Optional[str] = None
    designation: Optional[str] = None
    linkedin_url: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    source: Optional[str] = None


class Employee(SQLModel, table=True):
    __tablename__ = "employees"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    role: str
    skills: list = Field(default_factory=list, sa_column=Column(JSON))
    experience_summary: Optional[str] = None
    seniority: Optional[str] = None


class EmployeeMatch(SQLModel, table=True):
    __tablename__ = "employee_matches"

    id: Optional[int] = Field(default=None, primary_key=True)
    lead_id: int = Field(foreign_key="leads.id")
    employee_id: int = Field(foreign_key="employees.id")
    match_score: float
    ai_reasoning: Optional[str] = None
    confirmed: bool = Field(default=False)


class Email(SQLModel, table=True):
    __tablename__ = "emails"

    id: Optional[int] = Field(default=None, primary_key=True)
    lead_id: int = Field(foreign_key="leads.id")
    employee_id: Optional[int] = Field(default=None, foreign_key="employees.id")
    sender: Optional[str] = None
    recipient: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    status: str = Field(default="draft")  # draft | sent | failed
    sent_at: Optional[datetime] = None


class ActivityLog(SQLModel, table=True):
    __tablename__ = "activity_log"

    id: Optional[int] = Field(default=None, primary_key=True)
    entity_type: str
    entity_id: int
    action: str
    timestamp: datetime = Field(default_factory=utcnow)
