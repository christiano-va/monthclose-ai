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


# Columns added after the first create_all. SQLite does not alter tables itself.
_COLUMN_UPGRADES = {
    "document": {
        "original_filename": "VARCHAR",
        "direction": "VARCHAR",
        "referenced_invoice": "VARCHAR",
        "lines_json": "TEXT",
    },
    "openitem": {
        "assignee": "VARCHAR",
        "draft_email": "TEXT",
    },
}


def _upgrade_columns():
    with engine.begin() as connection:
        for table, columns in _COLUMN_UPGRADES.items():
            found = connection.exec_driver_sql("PRAGMA table_info(%s)" % table).fetchall()
            names = set()
            for row in found:
                names.add(row[1])
            for name, ddl in columns.items():
                if name not in names:
                    connection.exec_driver_sql(
                        "ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl)
                    )


def init_db():
    from app import models  # noqa: F401 — register tables

    SQLModel.metadata.create_all(engine)
    _upgrade_columns()


def get_session():
    with Session(engine) as session:
        yield session
