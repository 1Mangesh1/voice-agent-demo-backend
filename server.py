"""FastAPI server. Two endpoints:
  POST /token       → mint LiveKit JWT for browser to join a room
  POST /summary     → generate summary from CallSession.transcript via Gemini (<10s)
  GET  /session/{room} → fetch session record + appointments

Run: `uvicorn server:app --port 8000 --reload`
"""
import json
import os
import uuid
from datetime import datetime, timedelta
from typing import Optional

import google.generativeai as genai
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from livekit import api
from pydantic import BaseModel
from sqlmodel import select

from db import Appointment, CallSession, init_db, get_session

load_dotenv()
init_db()

genai.configure(api_key=os.getenv("GOOGLE_API_KEY", ""))

app = FastAPI(title="voice-agent-demo")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class TokenRequest(BaseModel):
    identity: Optional[str] = None
    room: Optional[str] = None


@app.post("/token")
def mint_token(req: TokenRequest) -> dict:
    identity = req.identity or f"user-{uuid.uuid4().hex[:8]}"
    room = req.room or f"call-{uuid.uuid4().hex[:8]}"
    key = os.getenv("LIVEKIT_API_KEY")
    secret = os.getenv("LIVEKIT_API_SECRET")
    if not key or not secret:
        raise HTTPException(500, "livekit creds not configured")
    token = (
        api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(api.VideoGrants(room=room, room_join=True, can_publish=True, can_subscribe=True))
        .with_ttl(timedelta(hours=1))
        .to_jwt()
    )
    return {"token": token, "url": os.getenv("LIVEKIT_URL"), "room": room, "identity": identity}


class SummaryRequest(BaseModel):
    room: str


@app.post("/summary")
def make_summary(req: SummaryRequest) -> dict:
    with get_session() as s:
        sess = s.exec(select(CallSession).where(CallSession.room_name == req.room)).first()
        if sess is None:
            raise HTTPException(404, "session not found")
        if sess.summary:
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
                model = genai.GenerativeModel("gemini-2.0-flash-exp")
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


def _build_summary_response(sess: CallSession, s) -> dict:
    appts = []
    if sess.user_phone:
        appts = s.exec(
            select(Appointment).where(Appointment.user_phone == sess.user_phone)
        ).all()
    return {
        "room": sess.room_name,
        "summary": sess.summary,
        "started_at": sess.started_at.isoformat() if sess.started_at else None,
        "ended_at": sess.ended_at.isoformat() if sess.ended_at else None,
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
