"""7 tools the voice agent calls. Pure functions over the DB.

Each tool returns a dict with `ok`, plus tool-specific fields.
The agent layer (agent.py) wraps these for LiveKit function-calling and emits
UI events on the data channel before/after each call.
"""
from datetime import datetime, timedelta
from typing import Optional
from sqlmodel import select
from db import User, Appointment, CallSession, get_session


def _normalize_phone(phone: str) -> str:
    return "".join(c for c in phone if c.isdigit() or c == "+")


# 1. identify_user ------------------------------------------------------------
def identify_user(phone: str, name: Optional[str] = None) -> dict:
    phone = _normalize_phone(phone)
    if len(phone.lstrip("+")) < 7:
        return {"ok": False, "error": "phone_too_short"}
    with get_session() as s:
        user = s.exec(select(User).where(User.phone == phone)).first()
        if user is None:
            user = User(phone=phone, name=name)
            s.add(user)
            s.commit()
            s.refresh(user)
            new = True
        else:
            if name and not user.name:
                user.name = name
                s.add(user)
                s.commit()
                s.refresh(user)
            new = False
        return {"ok": True, "phone": user.phone, "name": user.name, "new_user": new}


# 2. fetch_slots --------------------------------------------------------------
def fetch_slots(date: Optional[str] = None) -> dict:
    """Return up to 6 hardcoded slots for the given date (YYYY-MM-DD)
    or for tomorrow if not given. Excludes already-booked slots."""
    if date:
        try:
            base = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return {"ok": False, "error": "bad_date_format"}
    else:
        base = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    candidates = [base.replace(hour=h) for h in (9, 10, 11, 14, 15, 16)]
    iso = [c.strftime("%Y-%m-%dT%H:%M") for c in candidates]

    with get_session() as s:
        booked = s.exec(
            select(Appointment.slot).where(
                Appointment.status == "confirmed",
                Appointment.slot.in_(iso),  # type: ignore
            )
        ).all()
    booked_set = set(booked)
    free = [t for t in iso if t not in booked_set]
    return {"ok": True, "date": base.strftime("%Y-%m-%d"), "slots": free}


# 3. book_appointment ---------------------------------------------------------
def book_appointment(phone: str, slot: str) -> dict:
    phone = _normalize_phone(phone)
    try:
        datetime.strptime(slot, "%Y-%m-%dT%H:%M")
    except ValueError:
        return {"ok": False, "error": "bad_slot_format"}

    with get_session() as s:
        existing = s.exec(
            select(Appointment).where(
                Appointment.slot == slot, Appointment.status == "confirmed"
            )
        ).first()
        if existing:
            return {"ok": False, "error": "slot_taken"}
        user = s.exec(select(User).where(User.phone == phone)).first()
        if user is None:
            return {"ok": False, "error": "user_not_identified"}
        appt = Appointment(user_phone=phone, slot=slot, status="confirmed")
        s.add(appt)
        s.commit()
        s.refresh(appt)
        return {"ok": True, "id": appt.id, "phone": phone, "slot": slot}


# 4. retrieve_appointments ----------------------------------------------------
def retrieve_appointments(phone: str) -> dict:
    phone = _normalize_phone(phone)
    with get_session() as s:
        rows = s.exec(
            select(Appointment).where(Appointment.user_phone == phone).order_by(Appointment.slot)
        ).all()
        return {
            "ok": True,
            "appointments": [
                {"id": r.id, "slot": r.slot, "status": r.status} for r in rows
            ],
        }


# 5. cancel_appointment -------------------------------------------------------
def cancel_appointment(appointment_id: int) -> dict:
    with get_session() as s:
        appt = s.get(Appointment, appointment_id)
        if appt is None:
            return {"ok": False, "error": "not_found"}
        if appt.status == "cancelled":
            return {"ok": False, "error": "already_cancelled"}
        appt.status = "cancelled"
        s.add(appt)
        s.commit()
        return {"ok": True, "id": appointment_id}


# 6. modify_appointment -------------------------------------------------------
def modify_appointment(appointment_id: int, new_slot: str) -> dict:
    try:
        datetime.strptime(new_slot, "%Y-%m-%dT%H:%M")
    except ValueError:
        return {"ok": False, "error": "bad_slot_format"}
    with get_session() as s:
        appt = s.get(Appointment, appointment_id)
        if appt is None:
            return {"ok": False, "error": "not_found"}
        if appt.status != "confirmed":
            return {"ok": False, "error": "cannot_modify_cancelled"}
        clash = s.exec(
            select(Appointment).where(
                Appointment.slot == new_slot,
                Appointment.status == "confirmed",
                Appointment.id != appointment_id,
            )
        ).first()
        if clash:
            return {"ok": False, "error": "slot_taken"}
        appt.slot = new_slot
        s.add(appt)
        s.commit()
        return {"ok": True, "id": appointment_id, "slot": new_slot}


# 7. end_conversation ---------------------------------------------------------
def end_conversation(room_name: str) -> dict:
    """Marks call ended. Summary generated separately by /summary endpoint."""
    with get_session() as s:
        sess = s.exec(select(CallSession).where(CallSession.room_name == room_name)).first()
        if sess and sess.ended_at is None:
            sess.ended_at = datetime.utcnow()
            s.add(sess)
            s.commit()
    return {"ok": True, "room_name": room_name}
