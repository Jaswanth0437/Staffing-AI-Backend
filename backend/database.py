from sqlalchemy import inspect, text
from sqlmodel import SQLModel, Session, create_engine

from backend.config import settings
from backend import models  # noqa: F401  (registers tables with SQLModel.metadata)

engine = create_engine(settings.DATABASE_URL, echo=False, pool_pre_ping=True)


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """`create_all()` only creates missing tables, not missing columns on
    tables that already exist — there's no Alembic in this project, so new
    columns on an already-deployed table need a manual, idempotent ALTER
    here instead. Add an entry any time a model gains a field."""
    inspector = inspect(engine)
    if "jobs" in inspector.get_table_names():
        job_columns = {c["name"] for c in inspector.get_columns("jobs")}
        if "is_new" not in job_columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE jobs ADD COLUMN is_new BOOLEAN NOT NULL DEFAULT FALSE"))
    if "campaigns" in inspector.get_table_names():
        campaign_columns = {c["name"] for c in inspector.get_columns("campaigns")}
        if "last_run_summary" not in campaign_columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE campaigns ADD COLUMN last_run_summary JSON"))


def get_session():
    with Session(engine) as session:
        yield session
