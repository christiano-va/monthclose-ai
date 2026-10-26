from pathlib import Path

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "monthclose.db"
DATABASE_URL = "sqlite:///" + str(DB_PATH)

engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _enable_foreign_keys(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def init_db():
    from app import models  # noqa: F401 — register tables

    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
