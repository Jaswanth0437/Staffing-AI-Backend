from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/job2lead"
    APIFY_TOKEN: str = ""
    LINKEDIN_ACTOR_ID: str = ""  # ivanvs/linkedin-job-scraper
    DICE_ACTOR_ID: str = ""  # parsebird/dice-jobs-scraper
    APOLLO_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    SF_CLIENT_ID: str = ""
    SF_CLIENT_SECRET: str = ""
    SF_LOGIN_URL: str = ""  # e.g. https://login.salesforce.com or a my.salesforce.com sandbox/org domain
    AZURE_CLIENT_ID: str = ""
    AZURE_CLIENT_SECRET: str = ""
    AZURE_TENANT_ID: str = ""
    GRAPH_SENDER_EMAIL: str = ""


settings = Settings()

# Hardcoded demo user (no real auth for the hackathon build)
DEMO_USER = "demo@winfomi.com"

# --- Qualification rules (M2) ---
# Not every job source provides applicant_count / company_employee_size (e.g. the Dice
# actor has neither; LinkedIn has applicant_count but no company size). M2's rule engine
# must treat a null value as "unknown" and skip that specific check rather than reject
# the job outright — a missing field is not the same as a value that fails the threshold.
EMPLOYEE_SIZE_FLOOR = 10  # reject jobs at companies smaller than this (skipped if company_employee_size is null)
EMPLOYEE_SIZE_CEILING = 5000  # reject jobs at companies larger than this ("no MNC"); skipped if company_employee_size is null
APPLICANT_COUNT_CEILING = 100  # reject jobs with more applicants than this (skipped if applicant_count is null)
STAFFING_AGENCY_KEYWORDS = ["staffing", "recruiting", "recruitment", "talent solutions", "agency"]

# Caps results per Apify actor call (applied as maxRecords/maxResults in the actor
# input). Keeps demo campaigns small and bounds how many AI qualification calls a
# single campaign burns against Gemini's free-tier daily quota.
MAX_JOBS_PER_CAMPAIGN = 20

# Neither actor accepts `company` as a native search param (see apify_client.py's
# NET RESULT note), so the only way to raise the odds of the post-fetch company
# filter (backend/job_filters.py) actually matching something is pulling a much larger
# raw candidate pool to filter against. Only applied when search_criteria.company
# is set — role/location-only searches keep the smaller default volume above.
MAX_JOBS_PER_CAMPAIGN_WITH_COMPANY_FILTER = 120

# --- Post-fetch job filtering (M1) ---
# Neither Apify actor reliably enforces employment_type/work_mode natively (see the
# NET RESULT note in apify_client.py), so these are applied as an application-level
# text-match filter after both actors' results are merged, before jobs are inserted.
# Keys are the search_criteria values we accept; an unrecognized value falls back to
# a literal substring match of the value itself (with underscores turned to spaces).
EMPLOYMENT_TYPE_KEYWORDS = {
    "full_time": ["full-time", "full time", "fulltime"],
    "part_time": ["part-time", "part time", "parttime"],
    "contract": ["contract", "contractor", "c2c", "1099"],
    "internship": ["intern", "internship"],
    "temporary": ["temporary", "temp"],
}
WORK_MODE_KEYWORDS = {
    "remote": ["remote", "work from home", "wfh"],
    "hybrid": ["hybrid"],
    "onsite": ["on-site", "onsite", "on site", "in-office", "in office"],
}

# --- Email generation (M5) ---
COMPANY_NAME = "Winfomi Technologies"
COMPANY_SERVICES = ["staff augmentation", "contract-to-hire", "dedicated engineering teams"]
COMPANY_PITCH_TONE = "concise, consultative, no hard-sell"

# Internal notification recipient — gets a copy of every lead outreach email
# plus the employee match details behind it (unlike the lead-facing email,
# this one is allowed to name the actual matched employee).
CEO_NOTIFICATION_EMAIL = "jashwanth.m@winfomi.com"

# --- Employee matching (M3) ---
TOP_N_EMPLOYEE_MATCHES = 5
SALESFORCE_EMPLOYEE_OBJECT = "Resource__c"
