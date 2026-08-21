import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.config import settings
from backend.database import create_db_and_tables
from backend.routers import admin, campaigns, contacts, emails, employees, jobs, leads

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Job-to-Lead AI Outreach Platform")

# The deployed frontend (Vercel) and this API (Render) are different
# origins, so the browser needs an explicit CORS allow rather than the
# same-origin Next.js rewrite proxy used for local dev. allow_origin_regex
# additionally covers Vercel's per-branch/PR preview subdomains, which
# don't match a fixed origin in ALLOWED_ORIGINS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.ALLOWED_ORIGINS.split(",") if origin.strip()],
    allow_origin_regex=r"https://staffing-ai-frontend.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(campaigns.router)
app.include_router(jobs.router)
app.include_router(leads.router)
app.include_router(contacts.router)
app.include_router(emails.router)
app.include_router(employees.router)
app.include_router(admin.router)


@app.on_event("startup")
def on_startup() -> None:
    create_db_and_tables()


@app.get("/health")
def health():
    return {"status": "ok"}
