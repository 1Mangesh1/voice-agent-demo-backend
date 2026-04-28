"""SQLModel over Postgres (Supabase) or SQLite. Tables: User, Appointment, CallSession.

Set DATABASE_URL to a Supabase Postgres URI to use the cloud DB:
  postgresql://postgres.<ref>:<pwd>@aws-0-<region>.pooler.supabase.com:6543/postgres
Falls back to local SQLite (`sqlite:///app.db`) when unset.
"""
import os
from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field, create_engine, Session, select

# Resolution order:
#   1. DATABASE_URL (any SQLAlchemy-style URL).
#   2. SUPABASE_URL — accepted iff it is a Postgres URI (i.e. starts with
#      "postgres" rather than "https"). Lets users paste their Supabase
#      Session-pooler connection string under the SUPABASE_URL name.
#   3. Local SQLite fallback for tests / dev without keys.
_raw_url = (os.getenv("DATABASE_URL") or "").strip()
if not _raw_url:
    _su = (os.getenv("SUPABASE_URL") or "").strip()
    if _su.startswith("postgres"):
        _raw_url = _su
if not _raw_url:
    _raw_url = "sqlite:///app.db"

# Normalize Supabase / Heroku-style URIs to use the psycopg v3 driver explicitly.
if _raw_url.startswith("postgres://"):
    _raw_url = "postgresql+psycopg://" + _raw_url[len("postgres://"):]
elif _raw_url.startswith("postgresql://") and "+psycopg" not in _raw_url:
    _raw_url = "postgresql+psycopg://" + _raw_url[len("postgresql://"):]
DATABASE_URL = _raw_url

_is_sqlite = DATABASE_URL.startswith("sqlite")
engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=not _is_sqlite,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    phone: str = Field(unique=True, index=True)
    name: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Appointment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_phone: str = Field(index=True)
    slot: str  # ISO datetime string, e.g. "2026-05-02T10:00"
    status: str = Field(default="confirmed")  # confirmed | cancelled
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CallSession(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    room_name: str = Field(index=True)
    user_phone: Optional[str] = None
    transcript: str = Field(default="")  # JSON list of {role, text, ts}
    summary: Optional[str] = None
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Session:
    return Session(engine)
