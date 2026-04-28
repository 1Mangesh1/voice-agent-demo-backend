"""SQLite via SQLModel. Tables: User, Appointment, CallSession."""
import os
from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field, create_engine, Session, select

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///app.db")
engine = create_engine(DATABASE_URL, echo=False, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})


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
