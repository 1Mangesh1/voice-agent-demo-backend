"""Tavus CVI client.

Tavus runs the talking head + STT + LLM + TTS as one pipeline. The frontend
joins a Daily room (returned by `create_conversation`) and listens for
`conversation.tool_call` app-messages — Tavus does NOT execute tools server-
side. The frontend dispatches the tool to our backend, then broadcasts the
result back to Tavus via a `conversation.respond` Daily app-message.

This module:
  - holds the 7-tool OpenAI-style schema
  - get-or-create a "mira-clinic" Persona in the Tavus account
  - create / end conversations
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Optional

import httpx

log = logging.getLogger("tavus")

API_ROOT = "https://tavusapi.com/v2"
PERSONA_NAME = "mira-clinic"


def _key() -> str:
    k = (os.getenv("TAVUS_API_KEY") or "").strip()
    if not k:
        raise RuntimeError("TAVUS_API_KEY not set")
    return k


def _headers() -> dict[str, str]:
    return {"Content-Type": "application/json", "x-api-key": _key()}


# ---- Tool schema (OpenAI function-calling format) -------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "identify_user",
            "description": (
                "Look up or register the caller by phone number. "
                "Always call this first, before any other tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {"type": "string", "description": "Digits only, 7-15 chars"},
                    "name": {"type": "string", "description": "Caller's name if given"},
                },
                "required": ["phone"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_slots",
            "description": "List available appointment slots for a date (YYYY-MM-DD). Defaults to tomorrow.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD; omit for tomorrow"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": "Confirm a booking. Slot must be one returned by fetch_slots.",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {"type": "string"},
                    "slot": {"type": "string", "description": "YYYY-MM-DDTHH:MM"},
                },
                "required": ["phone", "slot"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retrieve_appointments",
            "description": "List appointments on file for the caller.",
            "parameters": {
                "type": "object",
                "properties": {"phone": {"type": "string"}},
                "required": ["phone"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_appointment",
            "description": "Cancel an appointment by its numeric id.",
            "parameters": {
                "type": "object",
                "properties": {"appointment_id": {"type": "integer"}},
                "required": ["appointment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "modify_appointment",
            "description": "Move an appointment to a new slot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "integer"},
                    "new_slot": {"type": "string", "description": "YYYY-MM-DDTHH:MM"},
                },
                "required": ["appointment_id", "new_slot"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_conversation",
            "description": "Wrap up the call once the caller is done.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


SYSTEM_PROMPT = (
    "You are Mira, the warm front-desk receptionist at a healthcare clinic. "
    "Greet the caller, then ASK FOR THEIR PHONE NUMBER and call identify_user "
    "before doing anything else. Keep replies short and conversational — this "
    "is a phone call, not a chat window. "
    "For booking: call fetch_slots, read out at most three options, confirm the "
    "caller's choice, then book_appointment. Read the date and time back clearly "
    "before confirming. For viewing or changing existing appointments, get the "
    "appointment id from retrieve_appointments first. When the caller is done, "
    "call end_conversation and say goodbye warmly. "
    "Tool results arrive as messages prefixed with `[tool_result]` followed by "
    "the tool name and a JSON payload. NEVER read these prefixed messages out "
    "loud — silently use the data to decide your next reply. If the JSON has "
    "ok=false, recover gracefully and ask the caller again in plain words. "
    "If a phone number sounds short, ask the caller to repeat it slowly digit "
    "by digit."
)


def _today_context() -> str:
    return f"Today's date is {datetime.utcnow().strftime('%Y-%m-%d')}."


# ---- Persona --------------------------------------------------------------

_persona_cache: Optional[str] = None


def get_or_create_persona() -> str:
    """Find a persona named PERSONA_NAME in the Tavus account, or create one.
    Cached in process memory; restarts will re-resolve from the API."""
    global _persona_cache
    if _persona_cache:
        return _persona_cache

    env_id = (os.getenv("TAVUS_PERSONA_ID") or "").strip()
    if env_id:
        _persona_cache = env_id
        log.info(f"using TAVUS_PERSONA_ID={env_id}")
        return env_id

    with httpx.Client(timeout=30.0) as c:
        r = c.get(f"{API_ROOT}/personas", headers=_headers())
        r.raise_for_status()
        for p in r.json().get("data", []):
            if p.get("persona_name") == PERSONA_NAME:
                _persona_cache = p["persona_id"]
                log.info(f"reusing existing persona {_persona_cache}")
                return _persona_cache  # type: ignore[return-value]

        body = {
            "persona_name": PERSONA_NAME,
            "system_prompt": SYSTEM_PROMPT,
            "context": _today_context(),
            "default_replica_id": (os.getenv("TAVUS_REPLICA_ID") or "").strip() or None,
            "pipeline_mode": "full",
            "layers": {"llm": {"tools": TOOLS}},
        }
        body = {k: v for k, v in body.items() if v is not None}
        r = c.post(f"{API_ROOT}/personas", headers=_headers(), json=body)
        if r.status_code >= 400:
            log.error(f"persona create failed: {r.status_code} {r.text}")
            r.raise_for_status()
        _persona_cache = r.json()["persona_id"]
        log.info(f"created persona {_persona_cache}")
        return _persona_cache  # type: ignore[return-value]


# ---- Conversation --------------------------------------------------------

def create_conversation(callback_url: Optional[str] = None) -> dict[str, Any]:
    persona_id = get_or_create_persona()
    replica_id = (os.getenv("TAVUS_REPLICA_ID") or "").strip()
    if not replica_id:
        raise RuntimeError("TAVUS_REPLICA_ID not set")

    body: dict[str, Any] = {
        "persona_id": persona_id,
        "replica_id": replica_id,
        "audio_only": False,
        "conversation_name": f"mira-{datetime.utcnow().isoformat(timespec='seconds')}",
        "conversational_context": _today_context(),
        "properties": {
            "max_call_duration": 600,
            "language": "english",
            "enable_recording": False,
            "enable_closed_captions": True,
            "apply_greenscreen": False,
        },
    }
    if callback_url:
        body["callback_url"] = callback_url

    with httpx.Client(timeout=30.0) as c:
        r = c.post(f"{API_ROOT}/conversations", headers=_headers(), json=body)
        if r.status_code >= 400:
            log.error(f"conversation create failed: {r.status_code} {r.text}")
            r.raise_for_status()
        return r.json()


def end_conversation(conversation_id: str) -> None:
    try:
        with httpx.Client(timeout=10.0) as c:
            c.post(f"{API_ROOT}/conversations/{conversation_id}/end", headers=_headers())
    except Exception as e:
        log.warning(f"end_conversation: {e}")
