from sqlmodel import SQLModel, Session, create_engine

from backend.config import settings
from backend import models  # noqa: F401  (registers tables with SQLModel.metadata)

engine = create_engine(settings.DATABASE_URL, echo=False, pool_pre_ping=True)


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
