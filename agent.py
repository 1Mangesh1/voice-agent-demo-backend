"""LiveKit voice agent. STT (Deepgram) + LLM (Gemini) + TTS (Cartesia).

Run: `python agent.py dev` (connects to LIVEKIT_URL, joins rooms on demand)
Or:  `python agent.py start` (production worker)

On every tool call:
- Sends a JSON event on the LiveKit data channel: {type:"tool", name, status, args, result}
  → Frontend listens and shows "Fetching slots..." / "Booking confirmed ✅".
- Logs transcript turns to CallSession.transcript (used by /summary endpoint).
"""
import json
import logging
import os
from datetime import datetime
from typing import Annotated, Optional

from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import Agent, AgentSession, JobContext, RoomInputOptions, function_tool
from livekit.plugins import cartesia, deepgram, google, silero
from sqlmodel import select

import tools as T
from db import CallSession, get_session, init_db

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("agent")

SYSTEM_PROMPT = """You are Mira, a warm front-desk AI for a healthcare clinic.

Goals:
1. Greet the caller. Ask their phone number first (used as ID). Call `identify_user`.
2. Ask their name if you don't have it.
3. Find out intent: book new / view existing / cancel / modify.
4. For booking: call `fetch_slots` to read out options, confirm choice, then `book_appointment`.
   ALWAYS read back date and time clearly before confirming.
5. For viewing: `retrieve_appointments`. Read out IDs and times.
6. For cancel/modify: get the appointment ID first, then call the tool.
7. When the caller is done, call `end_conversation` and say goodbye.

Style: short sentences. Conversational. No markdown. No long lists — read max 3 slots aloud at once.
If a tool returns ok=false, explain naturally and offer alternatives.
Today's date: """ + datetime.utcnow().strftime("%Y-%m-%d")


class FrontDeskAgent(Agent):
    def __init__(self, room: rtc.Room) -> None:
        super().__init__(instructions=SYSTEM_PROMPT)
        self.room = room

    async def _emit(self, payload: dict) -> None:
        try:
            await self.room.local_participant.publish_data(
                json.dumps(payload).encode("utf-8"), reliable=True
            )
        except Exception as e:
            log.warning(f"data publish failed: {e}")

    # ---- Tools (LiveKit auto-registers via @function_tool) ----

    @function_tool
    async def identify_user(
        self,
        phone: Annotated[str, "Caller's phone number"],
        name: Annotated[Optional[str], "Caller's name if known"] = None,
    ) -> str:
        await self._emit({"type": "tool", "name": "identify_user", "status": "running", "args": {"phone": phone}})
        result = T.identify_user(phone, name)
        await self._emit({"type": "tool", "name": "identify_user", "status": "done", "result": result})
        return json.dumps(result)

    @function_tool
    async def fetch_slots(
        self,
        date: Annotated[Optional[str], "Date YYYY-MM-DD; tomorrow if omitted"] = None,
    ) -> str:
        await self._emit({"type": "tool", "name": "fetch_slots", "status": "running", "args": {"date": date}})
        result = T.fetch_slots(date)
        await self._emit({"type": "tool", "name": "fetch_slots", "status": "done", "result": result})
        return json.dumps(result)

    @function_tool
    async def book_appointment(
        self,
        phone: Annotated[str, "Caller phone (as identified)"],
        slot: Annotated[str, "Slot in format YYYY-MM-DDTHH:MM"],
    ) -> str:
        await self._emit({"type": "tool", "name": "book_appointment", "status": "running", "args": {"phone": phone, "slot": slot}})
        result = T.book_appointment(phone, slot)
        await self._emit({"type": "tool", "name": "book_appointment", "status": "done", "result": result})
        return json.dumps(result)

    @function_tool
    async def retrieve_appointments(
        self, phone: Annotated[str, "Caller phone"]
    ) -> str:
        await self._emit({"type": "tool", "name": "retrieve_appointments", "status": "running", "args": {"phone": phone}})
        result = T.retrieve_appointments(phone)
        await self._emit({"type": "tool", "name": "retrieve_appointments", "status": "done", "result": result})
        return json.dumps(result)

    @function_tool
    async def cancel_appointment(
        self, appointment_id: Annotated[int, "Appointment ID"]
    ) -> str:
        await self._emit({"type": "tool", "name": "cancel_appointment", "status": "running", "args": {"appointment_id": appointment_id}})
        result = T.cancel_appointment(appointment_id)
        await self._emit({"type": "tool", "name": "cancel_appointment", "status": "done", "result": result})
        return json.dumps(result)

    @function_tool
    async def modify_appointment(
        self,
        appointment_id: Annotated[int, "Appointment ID"],
        new_slot: Annotated[str, "New slot YYYY-MM-DDTHH:MM"],
    ) -> str:
        await self._emit({"type": "tool", "name": "modify_appointment", "status": "running", "args": {"appointment_id": appointment_id, "new_slot": new_slot}})
        result = T.modify_appointment(appointment_id, new_slot)
        await self._emit({"type": "tool", "name": "modify_appointment", "status": "done", "result": result})
        return json.dumps(result)

    @function_tool
    async def end_conversation(self) -> str:
        await self._emit({"type": "tool", "name": "end_conversation", "status": "running"})
        result = T.end_conversation(self.room.name)
        await self._emit({"type": "tool", "name": "end_conversation", "status": "done", "result": result})
        return json.dumps(result)


def _record_turn(room_name: str, role: str, text: str) -> None:
    with get_session() as s:
        sess = s.exec(select(CallSession).where(CallSession.room_name == room_name)).first()
        if sess is None:
            sess = CallSession(room_name=room_name, transcript="[]")
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


async def entrypoint(ctx: JobContext) -> None:
    init_db()
    await ctx.connect()
    log.info(f"connected to room {ctx.room.name}")

    session = AgentSession(
        stt=deepgram.STT(model="nova-3"),
        llm=google.LLM(model="gemini-2.0-flash-exp"),
        tts=cartesia.TTS(voice=os.getenv("CARTESIA_VOICE_ID") or None),
        vad=silero.VAD.load(),
    )

    @session.on("user_input_transcribed")
    def _on_user(ev):
        if getattr(ev, "is_final", False):
            _record_turn(ctx.room.name, "user", ev.transcript)

    @session.on("conversation_item_added")
    def _on_item(ev):
        item = getattr(ev, "item", None)
        if item and getattr(item, "role", None) == "assistant":
            text = getattr(item, "text_content", None) or ""
            if text:
                _record_turn(ctx.room.name, "assistant", text)

    await session.start(
        room=ctx.room,
        agent=FrontDeskAgent(ctx.room),
        room_input_options=RoomInputOptions(),
    )
    await session.generate_reply(
        instructions="Greet the caller warmly as Mira from the clinic. Ask for their phone number to look them up."
    )


if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))
