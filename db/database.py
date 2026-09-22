from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker


DB_DIR = Path(__file__).resolve().parent
_db_path_override = os.environ.get("MARKET_AI_DB_PATH", "").strip()
DB_PATH = (
    Path(_db_path_override).expanduser().resolve()
    if _db_path_override
    else DB_DIR / "market_signal.db"
)
DATABASE_URL = f"sqlite:///{DB_PATH.as_posix()}"


class Base(DeclarativeBase):
    pass


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def check_database() -> bool:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return True
