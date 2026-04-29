# voice-agent-demo-backend

Front-desk healthcare voice AI. LiveKit Agents + Deepgram STT + Cartesia TTS + Gemini LLM + Supabase Postgres (SQLite fallback for local dev).

## Layout

| File | Purpose |
| --- | --- |
| `agent.py` | LiveKit agent worker. STT→LLM→TTS loop. 7 function tools. Emits tool-call events on data channel. |
| `server.py` | FastAPI: `/token` (LiveKit JWT), `/summary` (Gemini summary), `/session/{room}`, `/health`. |
| `tools.py` | Pure DB tool implementations. Unit-tested. |
| `db.py` | SQLModel schema (User, Appointment, CallSession). Engine picks Supabase Postgres when `DATABASE_URL` is set, else SQLite. |
| `tests/` | pytest suite over tools. |
| `render.yaml` | Render blueprint: single free-tier web service. |
| `start.sh` | Runs `python agent.py start &` then `uvicorn server:app` in foreground — one process, two roles. Exists because Render's free plan doesn't include background workers. |

## Local run

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in keys
pytest                 # tools sanity check
uvicorn server:app --port 8000 --reload   # terminal 1
python agent.py dev                        # terminal 2
```

## Tools

| name | args | effect |
| --- | --- | --- |
| `identify_user` | phone, name? | upsert User |
| `fetch_slots` | date? | 6 hardcoded slots minus booked |
| `book_appointment` | phone, slot | insert if free; rejects double-book |
| `retrieve_appointments` | phone | list user's bookings |
| `cancel_appointment` | id | mark cancelled |
| `modify_appointment` | id, new_slot | reschedule if free |
| `end_conversation` | – | mark CallSession ended |

Every tool call is mirrored on the LiveKit data channel as `{type:"tool", name, status, args, result}` so the frontend can render "Booking…" / "Booking confirmed ✅" badges live.

## Summary

`POST /summary {room}` → reads `CallSession.transcript`, asks Gemini for 4–6 bullets + extracted info JSON. Cached on `CallSession.summary`. Target: <10s.

## Database

`DATABASE_URL` controls the engine.
- **Supabase Postgres** (prod): grab from Supabase → Settings → Database → Connection String → URI (Session pooler, port 6543). Paste verbatim. `db.py` rewrites `postgres://` / `postgresql://` to `postgresql+psycopg://` for the psycopg v3 driver.
- **SQLite** (local dev / tests): leave `DATABASE_URL` blank → `app.db` is created in CWD.

`init_db()` runs `SQLModel.metadata.create_all` on startup, so the three tables are created the first time the API or worker boots against a fresh Supabase project — no migrations needed for the demo.
