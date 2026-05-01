# voice-agent-demo-backend

FastAPI in front of Tavus CVI + Supabase Postgres + Gemini.

Browser does the call. Tavus runs voice + face. This service:

- spins up Tavus conversations
- runs the 7 booking tools
- stores transcript turns
- writes a Gemini summary at end of call

Tool execution lives on the **frontend** — Tavus broadcasts `conversation.tool_call` Daily app-messages, frontend POSTs them here, replies via `conversation.respond`.

## Endpoints

| | path | what |
| --- | --- | --- |
| POST | `/tavus/start` | open Tavus conversation, return Daily room |
| POST | `/tavus/event` | webhook sink, no-op |
| POST | `/tools/{name}` | run one tool |
| POST | `/transcript` | append utterance |
| POST | `/summary` | end Tavus conv + Gemini recap |
| GET  | `/session/{room}` | inspect a saved call |
| GET  | `/health` | liveness (also: warmup ping target) |

## The 7 tools

| name | args | guards |
| --- | --- | --- |
| identify_user | phone, name? | phone < 7 digits → reject |
| fetch_slots | date? | bad_date_format |
| book_appointment | phone, slot | slot_taken, user_not_identified |
| retrieve_appointments | phone | – |
| cancel_appointment | id | not_found, already_cancelled |
| modify_appointment | id, new_slot | not_found, slot_taken, cannot_modify_cancelled |
| end_conversation | – | – |

Pure functions in `tools.py`. No HTTP, no Tavus. Tested directly.

## Layout

| file | what |
| --- | --- |
| `server.py` | FastAPI routes |
| `tavus.py` | Tavus REST client + persona + tool schema |
| `tools.py` | 7 booking funcs |
| `db.py` | SQLModel: User, Appointment, CallSession |
| `start.sh` | `exec uvicorn …` for Render |
| `render.yaml` | one free-tier web service |
| `tests/test_tools.py` | 10 cases over the 7 tools |

## Run local

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill keys
pytest                 # 10 green
uvicorn server:app --reload
```

`.env` needs:

```
TAVUS_API_KEY=...
TAVUS_REPLICA_ID=r90bbd427f71      # Anna (stock female, Phoenix-4)
TAVUS_PERSONA_ID=                  # blank → server creates "mira-clinic" on first call
PUBLIC_BASE_URL=http://localhost:8000
GOOGLE_API_KEY=...                 # Gemini for /summary
DATABASE_URL=postgresql://…        # blank → SQLite fallback
USD_TO_INR=83                      # cost display
TAVUS_RATE_USD_PER_MIN=0.15
```

## DB notes

`init_db()` runs `create_all` on boot. Three tables appear on first hit. No migrations.

Supabase on Render free tier → use the **Session pooler** URL (port 5432, `aws-…pooler.supabase.com`). Direct host is IPv6, free dyno can't reach.

## Known sharp edges

- Tavus does not run tools server-side. Frontend dispatches.
- Personas reused by name (`mira-clinic`). Restart re-resolves via API. No duplicates.
- Render free dyno sleeps after 15 min idle. Frontend pings `/health` on landing to warm it.
