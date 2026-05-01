"""FastAPI server.

Endpoints:
  POST /tavus/start           → create Tavus conversation; return Daily room URL
  POST /tavus/event           → Tavus webhook for transcripts + lifecycle
  POST /tools/{name}          → execute one of the 7 tools (called by frontend
                                when Tavus emits a tool_call app-message)
  POST /summary               → Gemini-generated post-call summary (<10s)
  GET  /session/{room}        → fetch session record + appointments
  GET  /health                → liveness
"""
import json
import logging
import os
from datetime import datetime
from typing import Any, Optional

import google.generativeai as genai
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlmodel import select

import tavus
import tools as T
from db import Appointment, CallSession, init_db, get_session

load_dotenv()
log = logging.getLogger("server")

try:
    init_db()
except Exception as e:  # pragma: no cover
    log.warning(f"init_db deferred: {e}")

genai.configure(api_key=os.getenv("GOOGLE_API_KEY", ""))

app = FastAPI(title="voice-agent-demo")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- Tavus -----------------------------------------------------------------

def _public_base() -> str:
    return (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")


@app.post("/tavus/start")
def tavus_start() -> dict:
    base = _public_base()
    callback = f"{base}/tavus/event" if base else None
    try:
        conv = tavus.create_conversation(callback_url=callback)
    except Exception as e:
        log.exception("tavus start failed")
        raise HTTPException(502, f"tavus_start_failed: {e}")

    room = conv["conversation_id"]
    with get_session() as s:
        if not s.exec(select(CallSession).where(CallSession.room_name == room)).first():
            s.add(CallSession(room_name=room, transcript="[]"))
            s.commit()
    return {
        "conversation_id": room,
        "conversation_url": conv["conversation_url"],
        "meeting_token": conv.get("meeting_token"),
    }


@app.post("/tavus/event")
async def tavus_event(req: Request) -> dict:
    """Best-effort transcript capture from Tavus webhooks. Tool calls are
    handled on the frontend (per Tavus's design); we just persist utterances."""
    try:
        body = await req.json()
    except Exception:
        return {"ok": True}

    event_type = body.get("event_type") or body.get("message_type") or ""
    conv_id = body.get("conversation_id")
    props = body.get("properties") or {}

    if event_type.endswith("utterance") and conv_id:
        role = props.get("role") or props.get("speaker") or "user"
        text = props.get("speech") or props.get("text") or ""
        if text:
            _record_turn(conv_id, "assistant" if role.startswith("repl") else "user", text)

    return {"ok": True}


# ---- 7 tools (called by frontend on Tavus tool_call) -----------------------

class ToolPayload(BaseModel):
    args: dict[str, Any] = {}
    conversation_id: Optional[str] = None


_TOOL_FNS = {
    "identify_user": lambda a: T.identify_user(a.get("phone", ""), a.get("name")),
    "fetch_slots": lambda a: T.fetch_slots(a.get("date")),
    "book_appointment": lambda a: T.book_appointment(a.get("phone", ""), a.get("slot", "")),
    "retrieve_appointments": lambda a: T.retrieve_appointments(a.get("phone", "")),
    "cancel_appointment": lambda a: T.cancel_appointment(int(a.get("appointment_id", 0))),
    "modify_appointment": lambda a: T.modify_appointment(
        int(a.get("appointment_id", 0)), a.get("new_slot", "")
    ),
}


@app.post("/tools/{name}")
def run_tool(name: str, payload: ToolPayload) -> dict:
    if name == "end_conversation":
        result = T.end_conversation(payload.conversation_id or "")
    else:
        fn = _TOOL_FNS.get(name)
        if fn is None:
            raise HTTPException(404, f"unknown tool: {name}")
        try:
            result = fn(payload.args)
        except Exception as e:
            log.exception(f"tool {name} failed")
            result = {"ok": False, "error": str(e)}

    if name == "identify_user" and result.get("ok") and payload.conversation_id:
        with get_session() as s:
            sess = s.exec(
                select(CallSession).where(CallSession.room_name == payload.conversation_id)
            ).first()
            if sess and not sess.user_phone:
                sess.user_phone = result.get("phone")
                s.add(sess)
                s.commit()

    return {"name": name, "result": result}


# ---- Transcript ingestion (frontend posts every utterance) -----------------

class TranscriptAppend(BaseModel):
    conversation_id: str
    role: str
    text: str


@app.post("/transcript")
def append_transcript(t: TranscriptAppend) -> dict:
    if t.text.strip():
        _record_turn(t.conversation_id, t.role, t.text)
    return {"ok": True}


def _record_turn(room: str, role: str, text: str) -> None:
    with get_session() as s:
        sess = s.exec(select(CallSession).where(CallSession.room_name == room)).first()
        if sess is None:
            sess = CallSession(room_name=room, transcript="[]")
            s.add(sess)
            s.commit()
            s.refresh(sess)
        try:
            arr = json.loads(sess.transcript or "[]")
        except json.JSONDecodeError:
            arr = []
        arr.append({"role": role, "text": text, "ts": datetime.utcnow().isoformat()})
        sess.transcript = json.dumps(arr)
        s.add(sess)
        s.commit()


# ---- Summary ---------------------------------------------------------------

class SummaryRequest(BaseModel):
    room: str


@app.post("/summary")
def make_summary(req: SummaryRequest) -> dict:
    with get_session() as s:
        sess = s.exec(select(CallSession).where(CallSession.room_name == req.room)).first()
        if sess is None:
            raise HTTPException(404, "session not found")
        if sess.summary and not sess.summary.startswith("(summary failed:"):
            return _build_summary_response(sess, s)

        try:
            turns = json.loads(sess.transcript or "[]")
        except json.JSONDecodeError:
            turns = []
        if not turns:
            sess.summary = "No conversation recorded."
        else:
            transcript_str = "\n".join(f"{t['role']}: {t['text']}" for t in turns)
            prompt = (
                "Summarize this front-desk healthcare call in 4-6 bullet points. "
                "Then list extracted info as JSON: name, phone, intent, preferences. "
                "Format strictly as:\n\nSUMMARY:\n- ...\n- ...\n\nINFO_JSON:\n{...}\n\n"
                f"Transcript:\n{transcript_str}"
            )
            try:
                model = genai.GenerativeModel("gemini-2.5-flash")
                resp = model.generate_content(prompt)
                sess.summary = resp.text
            except Exception as e:
                sess.summary = f"(summary failed: {e})\n\nRaw transcript:\n{transcript_str}"

        if sess.ended_at is None:
            sess.ended_at = datetime.utcnow()
        s.add(sess)
        s.commit()
        s.refresh(sess)
        return _build_summary_response(sess, s)


TAVUS_RATE_USD_PER_MIN = float(os.getenv("TAVUS_RATE_USD_PER_MIN", "0.15"))


def _build_summary_response(sess: CallSession, s) -> dict:
    appts = []
    if sess.user_phone:
        appts = s.exec(
            select(Appointment).where(Appointment.user_phone == sess.user_phone)
        ).all()

    duration_s = None
    cost_usd = None
    if sess.started_at and sess.ended_at:
        duration_s = max(0, int((sess.ended_at - sess.started_at).total_seconds()))
        cost_usd = round((duration_s / 60.0) * TAVUS_RATE_USD_PER_MIN, 4)

    return {
        "room": sess.room_name,
        "summary": sess.summary,
        "started_at": sess.started_at.isoformat() if sess.started_at else None,
        "ended_at": sess.ended_at.isoformat() if sess.ended_at else None,
        "duration_seconds": duration_s,
        "cost_usd": cost_usd,
        "user_phone": sess.user_phone,
        "appointments": [
            {"id": a.id, "slot": a.slot, "status": a.status} for a in appts
        ],
    }


@app.get("/session/{room}")
def get_session_info(room: str) -> dict:
    with get_session() as s:
        sess = s.exec(select(CallSession).where(CallSession.room_name == room)).first()
        if sess is None:
            raise HTTPException(404, "not found")
        try:
            turns = json.loads(sess.transcript or "[]")
        except json.JSONDecodeError:
            turns = []
        return {
            "room": sess.room_name,
            "transcript": turns,
            "summary": sess.summary,
            "started_at": sess.started_at.isoformat() if sess.started_at else None,
            "ended_at": sess.ended_at.isoformat() if sess.ended_at else None,
        }


@app.get("/health")
def health() -> dict:
    return {"ok": True}
