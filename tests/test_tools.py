"""Unit tests for the 7 tools. Uses an in-memory SQLite per test."""
import os
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import pytest
from db import init_db, engine
from sqlmodel import SQLModel
import tools as T


@pytest.fixture(autouse=True)
def fresh_db():
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    yield


def test_identify_user_creates_then_finds():
    r = T.identify_user("+919999999999", "Mango")
    assert r["ok"] and r["new_user"] is True
    r2 = T.identify_user("+919999999999")
    assert r2["ok"] and r2["new_user"] is False
    assert r2["name"] == "Mango"


def test_identify_user_rejects_short():
    assert T.identify_user("123")["ok"] is False


def test_fetch_slots_default_tomorrow():
    r = T.fetch_slots()
    assert r["ok"]
    assert len(r["slots"]) == 6


def test_book_then_no_double_book():
    T.identify_user("+911111111111")
    r = T.fetch_slots("2026-05-10")
    slot = r["slots"][0]
    b = T.book_appointment("+911111111111", slot)
    assert b["ok"]
    b2 = T.book_appointment("+911111111111", slot)
    assert b2["ok"] is False
    assert b2["error"] == "slot_taken"


def test_book_requires_identified_user():
    r = T.book_appointment("+912222222222", "2026-05-10T09:00")
    assert r["ok"] is False
    assert r["error"] == "user_not_identified"


def test_retrieve_appointments():
    T.identify_user("+913333333333")
    T.book_appointment("+913333333333", "2026-05-10T09:00")
    T.book_appointment("+913333333333", "2026-05-10T10:00")
    r = T.retrieve_appointments("+913333333333")
    assert r["ok"]
    assert len(r["appointments"]) == 2


def test_cancel_then_modify_blocked():
    T.identify_user("+914444444444")
    b = T.book_appointment("+914444444444", "2026-05-10T11:00")
    aid = b["id"]
    c = T.cancel_appointment(aid)
    assert c["ok"]
    m = T.modify_appointment(aid, "2026-05-10T14:00")
    assert m["ok"] is False


def test_modify_to_clashing_slot():
    T.identify_user("+915555555555")
    a = T.book_appointment("+915555555555", "2026-05-10T09:00")
    b = T.book_appointment("+915555555555", "2026-05-10T10:00")
    m = T.modify_appointment(a["id"], "2026-05-10T10:00")
    assert m["ok"] is False
    assert m["error"] == "slot_taken"


def test_modify_happy_path():
    T.identify_user("+916666666666")
    b = T.book_appointment("+916666666666", "2026-05-10T15:00")
    m = T.modify_appointment(b["id"], "2026-05-10T16:00")
    assert m["ok"]
    assert m["slot"] == "2026-05-10T16:00"


def test_end_conversation_idempotent():
    r = T.end_conversation("room-x")
    assert r["ok"]
    r2 = T.end_conversation("room-x")
    assert r2["ok"]
