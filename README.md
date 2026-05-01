# voice-agent-demo-backend

FastAPI in front of Tavus CVI + Supabase Postgres + Gemini.

Browser does the call. Tavus runs voice + face. This service:

- spins up Tavus conversations
- runs the 7 booking tools
- stores transcript turns
- writes a Gemini summary at end of call

Tool execution lives on the **frontend** — Tavus broadcasts `conversation.tool_call` Daily app-messages, frontend POSTs them here, replies via `conversation.respond`.

## Live

- Frontend: https://voice-agent-demo-frontend.vercel.app
- Backend health: https://voice-agent-demo-api.onrender.com/health
- Frontend repo: https://github.com/1Mangesh1/voice-agent-demo-frontend
- Demo recording: _(coming Fri)_

## On the live demo

Tavus's free tier caps at 25 conversation-minutes/month. The demo recording walks through the full happy path — identify, fetch slots, book, retrieve, modify, cancel, summary. The live URL is functional but rate-limited; if a fresh session doesn't connect, the recording shows the same flow against the same backend.

In production this isn't a constraint — it's a free-tier artifact of the submission environment.

## Flow

```
 browser ─mic→ Daily room (managed by Tavus)
                   │
                   ▼
            Tavus replica  ──voice + face──→ browser ─speaker
                   │
            LLM emits tool_call as Daily app-message
                   │
                   ▼
          frontend dispatches → POST /tools/{name}
                                       │
                                       ▼
                               this service ←→ Supabase
                                       │
              JSON result → frontend → conversation.respond
                                                  │
                                                  ▼
                                       Tavus LLM continues
```

## Stack decision: Tavus CVI vs the suggested stack

The assignment **suggested** a multi-vendor pipeline:
LiveKit (transport) → Deepgram (STT) → LLM → Cartesia (TTS) → Tavus (avatar).

I started on that exact stack — the git history walks through it: a LiveKit Agents worker, Deepgram nova-3 with server-side endpointing, Cartesia sonic-english TTS, Gemini 2.5-flash LLM, all deployed on Render's free tier. It worked end-to-end. Then I switched to Tavus CVI, which collapses STT + LLM + TTS + talking avatar into a single integration.

**Why I switched:**

- One integration vs four — fewer failure modes, less glue code, less to break under demo conditions.
- Latency budget: Tavus CVI hits sub-3s end-to-end out of the box. Matching that with separate LiveKit + Deepgram + Cartesia adds orchestration latency I'd then have to hand-tune.
- Lip-sync is native (Phoenix-4 model). The earlier stack used a custom amplitude-driven SVG portrait — readable as "talking" but not the real thing.
- All assignment must-haves are still met: voice in/out, 7 tools called by the LLM, double-book guards, DB persistence (Supabase Postgres), live tool indicators, post-call summary.

**What I'd change for production:**

- Tavus CVI is great for fast iteration but locks you into Tavus's STT/LLM/TTS choices. For finer voice control or cost-per-minute optimisation at volume, the multi-vendor pipeline is worth its complexity.
- Real production would likely be: own the orchestration layer (LiveKit Agents pattern) so you can swap STT/TTS/LLM per call by language, latency, or cost. Tavus just for the avatar layer.
- Tool execution currently goes through Tavus's function-calling on the frontend. In production I'd move tool dispatch fully server-side for tighter observability and easier expansion (analytics, idempotency keys, audit trail).

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
- Render free dyno sleeps after 15 min idle. Frontend pings `/health` on landing to warm it. UptimeRobot every 5 min would be the prod fix.

## Reviewing this

Try without setup:
1. Open the live frontend URL.
2. Click **Start call**, grant mic.
3. Speak: "Hi, my number is nine eight seven six five four three two one zero, I'd like to book an appointment for tomorrow."
4. Mira identifies you (reads phone back digit-by-digit), reads slot options, books, confirms.
5. End the call → summary appears in <10s with bullet recap, on-file appointments, duration, cost (USD + INR).

Verify the tool layer in isolation:
```bash
pytest   # 10 cases, no Tavus / no HTTP, runs on SQLite in-memory
```

## Not implemented (and why)

- **Server-side tool execution.** Tavus's recommended pattern is frontend dispatch via Daily app-messages. Trade-off: simpler latency story, slightly worse observability. Production would move dispatch server-side and use Tavus only for STT/LLM/TTS/face.
- **HMAC on the Tavus webhook.** `/tavus/event` is a no-op sink right now (frontend owns the transcript). Production would HMAC verify per Tavus docs.
- **Connection pooling beyond Supabase defaults.** Single-tenant demo doesn't need it. Production would tune the SQLAlchemy pool per-replica.
- **Phone correction loop beyond confirm-once.** Mira reads the phone back and waits for "yes". Production would also accept "no, it's actually …" mid-flow more gracefully.
