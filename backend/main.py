import logging

from fastapi import FastAPI

from backend.database import create_db_and_tables
from backend.routers import admin, campaigns, contacts, emails, employees, jobs, leads

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Job-to-Lead AI Outreach Platform")

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
