# voice-agent-demo-backend

Tiny FastAPI service that wires Tavus CVI to a Postgres-backed clinic schedule.
The browser does the call; this service:

1. Asks Tavus for a fresh conversation room (one per call).
2. Executes the 7 booking tools when the frontend asks.
3. Stores transcript turns as they come in.
4. Writes a Gemini-generated summary at the end of the call.

Tavus runs the actual voice + video pipeline. The agent (Mira) lives there.
Function calling is dispatched on the **frontend** — Tavus broadcasts a
`conversation.tool_call` Daily app-message, the frontend forwards it to
`POST /tools/{name}` here, and posts the result back as a
`conversation.respond` event.

## Endpoints

| Method | Path | What it does |
| --- | --- | --- |
| POST | `/tavus/start` | Creates (or reuses) the Mira persona, opens a Tavus conversation, returns the Daily room URL. |
| POST | `/tavus/event` | Webhook sink — quiets Tavus's retry logic. The frontend owns the transcript. |
| POST | `/tools/{name}` | Runs one of the 7 tools and returns its result. |
| POST | `/transcript` | Frontend appends each utterance for the summary step. |
| POST | `/summary` | Calls Gemini on the stored transcript, returns 4–6 bullet recap + extracted info. |
| GET  | `/session/{room}` | Inspect a saved call. |
| GET  | `/health` | Liveness. |

## The 7 tools

| name | args | effect |
| --- | --- | --- |
| `identify_user` | phone, name? | upsert User; called first thing |
| `fetch_slots` | date? | 6 hardcoded slots for the day, minus booked ones |
| `book_appointment` | phone, slot | insert if free; rejects double-book |
| `retrieve_appointments` | phone | list user's bookings |
| `cancel_appointment` | id | mark cancelled |
| `modify_appointment` | id, new_slot | move it; rejects clashes |
| `end_conversation` | – | mark CallSession ended |

All seven live in `tools.py` as plain functions over SQLModel. They have no
knowledge of Tavus or HTTP — `server.py` and `tests/` use them directly.

## Layout

| File | Purpose |
| --- | --- |
| `server.py` | FastAPI routes |
| `tavus.py` | Tavus REST client + persona/tool definitions |
| `tools.py` | The 7 booking tools |
| `db.py` | SQLModel schema (User, Appointment, CallSession). Picks Postgres when `DATABASE_URL` is set, else SQLite. |
| `start.sh` | `exec uvicorn server:app …` for Render |
| `render.yaml` | Single free-tier web service |
| `tests/test_tools.py` | pytest over the 7 tools |

## Run it locally

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in keys
pytest                 # tools sanity check
uvicorn server:app --reload
```

`.env` needs:

```
TAVUS_API_KEY=...
TAVUS_REPLICA_ID=r…             # any CVI-enabled replica
TAVUS_PERSONA_ID=               # blank — server creates one named "mira-clinic" on first call
PUBLIC_BASE_URL=http://localhost:8000
GOOGLE_API_KEY=...              # for /summary
DATABASE_URL=postgresql://…     # or leave blank for SQLite
```

## Database

`init_db()` runs `SQLModel.metadata.create_all` on startup, so the three
tables appear the first time the API hits a fresh Supabase project — no
migrations.

For Supabase on Render's free tier, use the **Session pooler** URL (port 5432
on `aws-…pooler.supabase.com`). The direct host resolves to IPv6, which the
free dyno can't reach.

## Notes from the build

- The original LiveKit + Deepgram + Cartesia + Gemini pipeline worked end-to-end
  on Render, but Tavus collapses it into one external dependency and gives a
  real talking head. The git history has every step.
- Tavus does not execute tool calls server-side. That's deliberate: the frontend
  has UI state to update anyway, so it's the right place to dispatch.
- Personas are reused by name (`mira-clinic`) — restart the server, it lists
  personas and finds the existing one rather than creating duplicates.
